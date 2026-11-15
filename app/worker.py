import asyncio
import ctypes
import logging
import os
import random
import signal
import socket
import uuid
from datetime import datetime, timedelta, timezone

from .db import Database
from .logging_config import configure_logging
from .pipeline import JobRunner
from .services.notify import QuietHours, merge_closed_batches
from .services.relevance import cleanup_existing_items, dedupe_duplicate_items
from .settings import get_settings
from .utils import json_loads, now_iso, stable_json

log = logging.getLogger(__name__)
_MUTEX_HANDLE = None


def single_instance() -> bool:
    """同一个数据目录只允许一个 Worker。

    互斥量名带上数据目录的指纹：机器上同时存在两个实例（升级期间新旧并存、
    或者开发实例和正式实例一起开着）时，各自跑各自的，不会互相把对方挤掉。
    """
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    import hashlib
    from .paths import DATA_DIR
    tag = hashlib.sha1(str(DATA_DIR).casefold().encode("utf-8")).hexdigest()[:12]
    _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(
        None, False, f"Local\\HermesResearchRadarWorker-{tag}")
    return ctypes.windll.kernel32.GetLastError() != 183


def activate_pending_sources(db: Database) -> int:
    """One-shot activation for newly validated local sources after a code reload."""
    activated = 0
    for source in db.query("SELECT * FROM sources WHERE type IN ('wechat_local','wechat_public_local')"):
        config = json_loads(source["config_json"], {}) or {}
        if not config.pop("enable_after_restart", False):
            continue
        timestamp = now_iso()
        db.execute("""UPDATE sources SET enabled=1,next_poll_at=?,config_json=?,error_count=0,
            last_error=NULL,updated_at=? WHERE id=?""", (timestamp, stable_json(config), timestamp, source["id"]))
        db.add_event("source_activated", f"新版本已加载，自动启用：{source['name']}", source_id=source["id"])
        activated += 1
    return activated


class Worker:
    def __init__(self):
        self.db = Database()
        self.runner = JobRunner(self.db)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stop_event = asyncio.Event()

    def stop(self, *args) -> None:
        self.stop_event.set()

    async def scheduler(self) -> None:
        while not self.stop_event.is_set():
            try:
                for source in self.db.source_due():
                    interval = max(30, int(source["poll_seconds"] or 300))
                    outstanding = self.db.one("""SELECT id FROM jobs WHERE kind='poll_source'
                        AND state IN ('READY','RETRY_WAIT','LEASED')
                        AND json_extract(payload_json,'$.source_id')=?""", (source["id"],))
                    now = datetime.now(timezone.utc)
                    delay = interval + random.uniform(0, min(30.0, interval * 0.15))
                    next_poll = (now + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
                    if not outstanding:
                        window = int(now.timestamp() // interval)
                        self.db.enqueue_job("poll_source", {"version": 1, "source_id": source["id"]},
                                            dedupe_key=f"poll:{source['id']}:{window}", priority=90, max_attempts=5)
                    self.db.execute("UPDATE sources SET next_poll_at=?,updated_at=? WHERE id=?", (next_poll, now_iso(), source["id"]))
            except Exception:
                log.exception("调度来源失败")
            await asyncio.sleep(0.7)

    async def heartbeat(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.db.heartbeat("worker")
                self.db.recover_expired_jobs()
            except Exception:
                log.exception("Worker 心跳失败")
            await asyncio.sleep(3)

    async def jsonl_watcher(self) -> None:
        """JSONL is a local tail, not a poll job: avoid thousands of queue rows per day."""
        while not self.stop_event.is_set():
            try:
                sources = self.db.query("SELECT * FROM sources WHERE type='jsonl' AND enabled=1")
                for source in sources:
                    await self.runner._poll({"id": f"jsonl-watch:{source['id']}"},
                                            {"source_id": source["id"]})
            except Exception:
                log.exception("分享桥监听失败")
            await asyncio.sleep(2)

    async def loop(self, number: int) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.db.claim_job(self.owner)
            except Exception:
                log.exception("领取任务失败")
                await asyncio.sleep(2)
                continue
            if not job:
                await asyncio.sleep(0.35)
                continue
            log.info("执行 %s %s attempt=%s", job["kind"], job["id"], job["attempts"])
            payload = json_loads(job["payload_json"], {}) or {}
            item_id = payload.get("item_id")
            try:
                await self.runner.handle(job)
                self.db.complete_job(job["id"])
                if item_id:
                    self.db.clear_runtime(f"progress:{item_id}")
                if job["kind"] in {"archive_item", "papers_item", "analyze_item"}:
                    self.db.prune_stale_jobs()
            except QuietHours as exc:
                run_at = (datetime.now(timezone.utc) + timedelta(seconds=exc.delay_seconds)).isoformat(timespec="milliseconds")
                # A scheduled wait must not consume the retry budget or the counter
                # climbs into the hundreds while the job is simply waiting for 20:30.
                self.db.reschedule_job(job["id"], run_at, reason=str(exc), reset_attempts=True)
            except Exception as exc:
                log.exception("任务失败 %s", job["id"])
                text = f"{type(exc).__name__}: {exc}"
                # A timed-out reading will not get faster on the fourth try; report it instead of
                # leaving the item spinning in "精读中" for another ten minutes.
                give_up = job["kind"] == "analyze_item" and int(job["attempts"]) >= 2 and (
                    isinstance(exc, TimeoutError) or "request_invalid_parameter" in text)
                state = self.db.retry_job(job, text, force_dead=give_up)
                if item_id:
                    self.db.clear_runtime(f"progress:{item_id}")
                if state == "DEAD":
                    if job["kind"] in {"archive_item", "papers_item", "analyze_item"} and payload.get("item_id"):
                        self.db.fail_item(payload["item_id"], f"{job['kind']} 多次失败：{exc}")
                    if job["kind"] == "deliver_batch" and payload.get("batch_id"):
                        self.db.execute("UPDATE notification_batches SET last_error=? WHERE id=?",
                                        (str(exc)[:4000], payload["batch_id"]))
            await asyncio.sleep(0)

    async def run(self) -> None:
        activated = activate_pending_sources(self.db)
        if activated:
            log.info("新版本首次启动，自动启用 %s 个本地微信来源", activated)
        cleaned = cleanup_existing_items(self.db)
        if cleaned["excluded"]:
            log.info("启动科研复核：移出 %s 条非目标内容", cleaned["excluded"])
        if cleaned["restored"]:
            log.info("启动科研复核：恢复 %s 条此前被误过滤的资料", cleaned["restored"])
        duplicates = dedupe_duplicate_items(self.db)
        if duplicates:
            log.info("重复链接整理：合并隐藏 %s 条重复副本", duplicates)
        recovered = self.db.recover_expired_jobs()
        pruned = self.db.prune_stale_jobs()
        if pruned:
            log.info("已清理 %s 条过期的失败任务记录", pruned)
        pruned_batches = self.db.prune_notification_history()
        if pruned_batches:
            log.info("已清理 %s 条过期汇报记录", pruned_batches)
        closed_before = self.db.one("""SELECT COUNT(*) c FROM notification_batches
            WHERE state='CLOSED' AND priority='normal'""")["c"]
        merged_batch = merge_closed_batches(self.db)
        if merged_batch and closed_before > 1:
            log.info("汇报批次已合并为 %s，队列中只保留一条发送任务", merged_batch)
        log.info("Worker 启动 owner=%s recovered=%s", self.owner, recovered)
        concurrency = max(1, min(8, int(get_settings()["worker_concurrency"])))
        tasks = [asyncio.create_task(self.scheduler()), asyncio.create_task(self.heartbeat()),
                 asyncio.create_task(self.jsonl_watcher())]
        tasks += [asyncio.create_task(self.loop(i)) for i in range(concurrency)]
        await self.stop_event.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    configure_logging("worker")
    if not single_instance():
        log.warning("Worker 已在运行，本实例退出")
        return
    worker = Worker()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, worker.stop)
        except (ValueError, OSError):
            pass
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
