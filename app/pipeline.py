import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .collectors import poll_source
from .db import Database, row_with_json
from .services.analyzer import analyze_item
from .services.archive import SourceGone, archive_item
from .services.notify import deliver_batch, flush_open_batches
from .services.papers import process_item_papers
from .services.relevance import assess_candidate, detect_marketing
from .services.filters import load_rules, match as match_rules
from .utils import ARCHIVE_PAUSE_KEY, now_iso, redact_secrets, stable_json

log = logging.getLogger(__name__)


def start_archive_batch(db: Database) -> dict[str, Any]:
    """手动开始「归档全部仅索引条目」。这一步同时是「继续」：先撤掉暂停标记。"""
    remaining = db.one("SELECT COUNT(*) c FROM items WHERE status='discovered' AND excluded_at IS NULL")["c"]
    if not remaining:
        return {"queued": False, "remaining": 0}
    db.clear_runtime(ARCHIVE_PAUSE_KEY)
    running = db.one("""SELECT id FROM jobs WHERE kind='archive_history_batch'
        AND state IN ('READY','RETRY_WAIT','LEASED')""")
    if running:
        return {"queued": True, "already_running": True, "remaining": remaining, "llm_calls": 0}
    db.enqueue_job("archive_history_batch", {"version": 1, "batch_size": 8},
                   dedupe_key=f"history-archive-start:all:{uuid.uuid4()}", priority=200, max_attempts=3)
    return {"queued": True, "remaining": remaining, "llm_calls": 0}


def stop_archive_batch(db: Database) -> dict[str, Any]:
    """叫停批量归档：撤销排队中的批次，并让正在跑的那一批做完当前这条就收手。

    不硬杀正在执行的协程——它可能正卡在一次 HTTP 下载里，硬杀会把条目留在半截
    状态。所以走「标记 + 排空」：置暂停标记 → 取消还没被领走的批次任务 → 正在跑
    的那一批在条目之间检查标记，做完手上这条就退出，不再续排。

    放在这里而不是直接写进 API，是因为它是纯数据库操作，测试不该为了调它去
    import 整个 FastAPI 应用。
    """
    db.set_runtime(ARCHIVE_PAUSE_KEY, now_iso())
    cancelled = db.execute("""UPDATE jobs SET state='CANCELLED', updated_at=?
        WHERE kind='archive_history_batch' AND state IN ('READY','RETRY_WAIT')""",
        (now_iso(),))
    alive = db.one("""SELECT COUNT(*) c FROM jobs WHERE kind='archive_history_batch' AND state='LEASED'""")["c"]
    remaining = db.one("""SELECT COUNT(*) c FROM items WHERE status='discovered' AND excluded_at IS NULL""")["c"]
    db.add_event("archive_paused",
                 f"已暂停批量归档：撤销 {cancelled} 个排队批次，正在执行的一批会在当前这条结束后停下；"
                 f"还剩 {remaining} 条未归档")
    return {"paused": True, "cancelled": cancelled, "running_batches": alive, "remaining": remaining}


class JobRunner:
    def __init__(self, db: Database):
        self.db = db

    async def handle(self, job: dict[str, Any]) -> None:
        from .utils import json_loads
        payload = json_loads(job["payload_json"], {}) or {}
        kind = job["kind"]
        if kind == "poll_source":
            await self._poll(job, payload)
        elif kind == "archive_item":
            await self._archive(payload["item_id"], archive_only=bool(payload.get("archive_only")))
        elif kind == "papers_item":
            await process_item_papers(self.db, payload["item_id"], archive_only=bool(payload.get("archive_only")),
                                      skip_analysis=bool(payload.get("skip_analysis")))
        elif kind == "archive_history_batch":
            await self._archive_history_batch(payload)
        elif kind == "analyze_item":
            from .services.analyzer import ensure_daily_budget
            ensure_daily_budget(self.db)          # 没额度就别抓正文，整单顺延
            await self._archive_if_missing(payload["item_id"])
            if payload.get("force"):
                await self._refresh_papers(payload["item_id"])
            await analyze_item(self.db, payload["item_id"], force=bool(payload.get("force")))
        elif kind == "flush_batch":
            flush_open_batches(self.db)
        elif kind == "deliver_batch":
            await deliver_batch(self.db, payload["batch_id"])
        else:
            raise ValueError(f"未知任务类型：{kind}")

    async def _poll(self, job: dict[str, Any], payload: dict[str, Any]) -> None:
        source = self.db.one("SELECT * FROM sources WHERE id=?", (payload["source_id"],))
        if not source or not source["enabled"]:
            return
        try:
            candidates, cursor = await poll_source(source, job["id"])
            new_count = filtered_count = rule_count = 0
            rules = load_rules(self.db)
            for candidate in candidates:
                assessment = assess_candidate(candidate)
                if not assessment["keep"]:
                    filtered_count += 1
                    continue
                if rules:
                    metadata = candidate.get("metadata") or {}
                    blocked = match_rules(rules, candidate.get("author"), candidate.get("title"),
                                          metadata.get("description"))
                    if blocked:
                        # 用户自己定的规则优先于一切：不想看的栏目连索引都不留。
                        rule_count += 1
                        continue
                metadata = candidate.setdefault("metadata", {})
                metadata["admission_filter"] = {"reason": assessment["reason"], "matched_terms": assessment["matched_terms"]}
                _, created = self.db.discover_item(candidate)
                new_count += int(created)
            cursor["admission_filtered"] = filtered_count
            cursor["rule_filtered"] = rule_count
            cursor_changed = stable_json(cursor) != source.get("cursor_json")
            if source["type"] == "jsonl" and not candidates and not cursor_changed:
                return
            warnings = [str(x) for x in cursor.get("warnings", [])]
            warning_text = "；".join(warnings)[:3500] or None
            self.db.execute("""UPDATE sources SET last_poll_at=?,last_success_at=?,error_count=0,last_error=?,
                cursor_json=?,updated_at=? WHERE id=?""", (now_iso(), now_iso(), warning_text, stable_json(cursor), now_iso(), source["id"]))
            if cursor.get("backfill_pending"):
                next_poll = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="milliseconds")
                self.db.execute("UPDATE sources SET next_poll_at=? WHERE id=?", (next_poll, source["id"]))
            # Only real additions deserve an event. A poll that read the same twelve
            # links again used to write an identical line every minute.
            if new_count or not source.get("last_success_at"):
                detail = f"{source['name']}：新消息链接 {len(candidates)} 条，收录 {new_count} 条"
                if filtered_count:
                    detail += f"，本地过滤 {filtered_count} 条"
                if rule_count:
                    detail += f"，按你的过滤规则跳过 {rule_count} 条"
                self.db.add_event("source_polled", detail, source_id=source["id"])
            if warnings:
                self.db.add_event("source_warning", f"{source['name']}：{warning_text}", source_id=source["id"])
        except Exception as exc:
            message = redact_secrets(str(exc))
            self.db.execute("UPDATE sources SET last_poll_at=?,error_count=error_count+1,last_error=?,updated_at=? WHERE id=?",
                            (now_iso(), message[:4000], now_iso(), source["id"]))
            self.db.add_event("source_error", f"{source['name']}：{message}", source_id=source["id"])
            raise

    async def _archive_if_missing(self, item_id: str) -> None:
        """Manual re-reading on an index-only item must fetch the body first, not analyse a stub."""
        row = self.db.one("SELECT id,status,text,html_path,excluded_at FROM items WHERE id=?", (item_id,))
        if not row:
            raise ValueError("待精读条目不存在")
        if row.get("excluded_at"):
            return
        if row.get("html_path") and (row.get("text") or "").strip():
            return
        # The manual job already carries force=True; don't let the papers step queue a second reading.
        await self._archive(item_id, skip_analysis=True)

    async def _refresh_papers(self, item_id: str) -> None:
        """手动「重新精读」时，把论文匹配也重跑一遍。

        以前只重跑精读：正文里没有 DOI 的条目永远停在「没查到对应论文」，
        哪怕正文末尾就贴着 nature.com 的原文链接。现在会按链接和引号里的
        英文标题再找一次；已经挂上论文的条目不动，避免重复挂一篇错的进来。
        """
        from .settings import get_settings
        if not get_settings()["papers"].get("enabled", True):
            return
        row = self.db.one("""SELECT i.text,i.excluded_at,
                (SELECT COUNT(*) FROM item_papers ip WHERE ip.item_id=i.id) papers
            FROM items i WHERE i.id=?""", (item_id,))
        if not row or row["excluded_at"] or int(row["papers"] or 0) or not (row["text"] or "").strip():
            return
        try:
            await process_item_papers(self.db, item_id, archive_only=True)
        except Exception as exc:
            log.warning("重新匹配论文失败 item=%s：%s", item_id, exc)

    async def _archive(self, item_id: str, archive_only: bool = False, skip_analysis: bool = False) -> None:
        row = self.db.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not row:
            raise ValueError("待归档条目不存在")
        if row.get("excluded_at"):
            return
        item = row_with_json(row, "metadata_json", "tags_json", "analysis_json")
        try:
            result = await archive_item(item)
        except SourceGone as exc:
            self._source_gone(item_id, exc.reason)
            return
        self.db.update_archive(item_id, result)
        if (item.get("kind") or "article") == "article":
            verdict = detect_marketing(result.get("text") or "")
            if verdict["blocked"]:
                # 抓完才知道正文是软文：与其让它占着资料库，不如当场移出去。
                self.db.set_items_excluded([item_id], f"引流/营销文章：{verdict['reason']}")
                self.db.add_event("marketing_filter", f"归档时判定为引流软文：{verdict['reason']}", item_id)
                return
        # Triage right after archiving, not only when the item is read.  Otherwise a
        # queued article keeps its ad banners and group photos in the library until
        # someone happens to spend a reading call on it.
        from .services.analyzer import triage_archived_images
        try:
            await triage_archived_images(self.db, item_id)
        except Exception as exc:  # noqa: BLE001 - never block archiving on a vision call
            log.warning("归档后图片分诊跳过（%s）：%s", item_id, exc)
        self.db.enqueue_job("papers_item", {"version": 1, "item_id": item_id, "archive_only": archive_only,
                                            "skip_analysis": skip_analysis},
                            dedupe_key=f"papers:{item_id}:initial", priority=55, max_attempts=4)

    def _source_gone(self, item_id: str, reason: str) -> None:
        """Publisher deleted the page: keep it only when the offline copy survived."""
        from pathlib import Path
        from .utils import json_loads, stable_json
        row = self.db.one("SELECT id,text,html_path,excluded_at,metadata_json FROM items WHERE id=?", (item_id,))
        if not row or row.get("excluded_at"):
            return
        html_path = row.get("html_path") or ""
        archived = bool(html_path) and Path(html_path).is_file() and len((row.get("text") or "").strip()) >= 200
        if archived:
            metadata = json_loads(row.get("metadata_json"), {}) or {}
            metadata["source_status"] = {"state": "deleted", "reason": reason[:300],
                                         "checked_at": now_iso()}
            self.db.execute("UPDATE items SET metadata_json=?,error=NULL WHERE id=? AND excluded_at IS NULL",
                            (stable_json(metadata), item_id))
            self.db.add_event("source_deleted", f"原文已被删除，本地离线页仍在，继续保留：{reason}", item_id)
            return
        self.db.set_items_excluded([item_id], f"原文已被删除且本地没有离线页：{reason}")
        self.db.add_event("source_deleted", f"原文已被删除且没有离线页，已移出资料库：{reason}", item_id)

    async def _archive_history_batch(self, payload: dict[str, Any]) -> None:
        source_id = payload.get("source_id")
        batch_size = max(1, min(20, int(payload.get("batch_size", 8))))

        def paused() -> bool:
            return bool(self.db.get_runtime(ARCHIVE_PAUSE_KEY))

        # 暂停期间被重新排进来的批次直接收手，不要白跑一轮再退出。
        if paused():
            log.info("批量归档已暂停，跳过本批次")
            return

        if source_id:
            rows = self.db.query("""SELECT id FROM items WHERE source_id=? AND status='discovered' AND excluded_at IS NULL
                ORDER BY COALESCE(published_at,discovered_at),id LIMIT ?""", (source_id, batch_size))
        else:
            rows = self.db.query("""SELECT id FROM items WHERE status='discovered' AND excluded_at IS NULL
                ORDER BY COALESCE(published_at,discovered_at),id LIMIT ?""", (batch_size,))
        completed = errors = 0
        for row in rows:
            # 用户按了停止：手上这条可能正卡在下载里，等它做完，下一条不再领。
            if paused():
                log.info("批量归档已暂停，做完当前条目后停止")
                break
            try:
                await self._archive(row["id"], archive_only=True)
                completed += 1
            except Exception as exc:
                errors += 1
                self.db.fail_item(row["id"], f"历史归档失败：{type(exc).__name__}: {exc}")
        if source_id:
            remaining = self.db.one("SELECT COUNT(*) c FROM items WHERE source_id=? AND status='discovered' AND excluded_at IS NULL",
                                    (source_id,))["c"]
        else:
            remaining = self.db.one("SELECT COUNT(*) c FROM items WHERE status='discovered' AND excluded_at IS NULL")["c"]
        if paused():
            self.db.add_event("history_archive_progress",
                              f"历史全文归档已暂停：本批完成 {completed}，失败 {errors}，剩余 {remaining}", source_id=source_id)
            return
        self.db.add_event("history_archive_progress",
                          f"历史全文归档：本批完成 {completed}，失败 {errors}，剩余 {remaining}；未调用云精读", source_id=source_id)
        if remaining:
            run_at = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(timespec="milliseconds")
            payload = {"version": 1, "batch_size": batch_size}
            if source_id:
                payload["source_id"] = source_id
            self.db.enqueue_job("archive_history_batch", payload,
                                run_at=run_at, dedupe_key=f"history-archive:{source_id or 'all'}:{uuid.uuid4()}",
                                priority=200, max_attempts=3)
