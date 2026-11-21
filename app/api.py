import csv
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, updater
from .collectors import COLLECTORS
from .db import Database, row_with_json
from .logging_config import configure_logging
from .paths import BLOB_DIR, DATA_DIR, LOG_DIR, WEB_DIR
from .services.notify import flush_open_batches, send_test_notification
from .services.relevance import assess_candidate
from .services import wechat_panel, wechat_public
from .settings import get_secret, get_settings, save_settings, secret_status, set_secret
from .utils import (ARCHIVE_PAUSE_KEY, extract_bvid, extract_urls, job_error_is_retryable, json_loads, local_date,
                    now_iso, parse_time, redact_secrets, stable_json, utcnow)

configure_logging("web")
db = Database()


def _check_updates_in_background() -> None:
    """开机后台查一次新版本，同一天不重复。

    只查不下载：装不装由用户在界面上决定。延迟 20 秒是为了别和采集器抢带宽，
    也别让首页首次渲染等它。
    """
    def worker() -> None:
        import time as _time
        _time.sleep(20)
        try:
            checked = str(updater.status().get("checked_at") or "")
            if checked[:10] == _time.strftime("%Y-%m-%d"):
                return
            updater.check()
        except Exception:  # noqa: BLE001 - 更新检查永远不该影响主流程
            pass

    threading.Thread(target=worker, daemon=True, name="update-check").start()


def _warm_setup_snapshot(delay: float = 8.0) -> None:
    """启动后在后台先把体检快照算一遍。

    第一次进「设置 → 配置向导」要读一次微信进程、扫一遍磁盘，冷算六秒；先在后台
    算好，用户点开时读的就是缓存。算不出来不影响任何功能，所以异常一律吞掉。
    """

    def run() -> None:
        time.sleep(delay)
        try:
            from .services import setup_status
            setup_status.snapshot()
        except Exception:                                   # noqa: BLE001
            pass

    threading.Thread(target=run, daemon=True, name="setup-snapshot").start()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _check_updates_in_background()
    _warm_setup_snapshot()
    yield


app = FastAPI(title="Hermes 科研雷达", version=__version__, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

# Files whose URL must change whenever their bytes change.  FastAPI's StaticFiles
# sends no Cache-Control at all, so the browser applied heuristic caching and kept
# running a days-old app.js: the double-click zoom and the detail-dialog guard were
# both shipped but never reached the page.
_ASSET_FILES = ("styles.css", "app.js", "assistant.js", "setup.js")


def _asset_version(name: str) -> str:
    try:
        return str(int((WEB_DIR / name).stat().st_mtime))
    except OSError:
        return "0"


@app.middleware("http")
async def revalidate_static_assets(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response



class IngestInput(BaseModel):
    text: str
    kind: str = "auto"


class SourceInput(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    poll_seconds: int = 300
    enabled: bool = True


class SourcePatch(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    poll_seconds: int | None = None
    enabled: bool | None = None


class WeChatAccountPatch(BaseModel):
    enabled: bool | None = None
    fit: str | None = None
    category_code: str | None = None


class WeChatArticle(BaseModel):
    """面板上的一张推文卡片。前端把卡片原样回传，避免二次打开数据库。"""

    url: str
    title: str = ""
    summary: str = ""
    cover: str = ""
    published_at: str | None = None
    message_id: int | None = None


class WeChatArticleAction(BaseModel):
    articles: list[WeChatArticle] = Field(default_factory=list)


class WeChatExcludeInput(WeChatArticleAction):
    mode: str = "one"
    reason: str = ""
    keywords: list[str] = Field(default_factory=list)
    account_match: str = ""
    disable_account: bool = False


class WeChatAccountBulkInput(BaseModel):
    ids: list[int]
    enabled: bool


class WeChatImportInput(BaseModel):
    names_text: str
    poll_seconds: int = 300
    initial_backfill_days: int = 30


class IdeaInput(BaseModel):
    title: str
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class SecretInput(BaseModel):
    name: str
    value: str


class NoteInput(BaseModel):
    item_id: str | None = None
    section: str = ""
    quote: str = ""
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False


class NotePatch(BaseModel):
    body: str | None = None
    tags: list[str] | None = None
    pinned: bool | None = None


class ChatInput(BaseModel):
    title: str = ""
    model: str = ""
    item_id: str | None = None


class ChatPatch(BaseModel):
    title: str | None = None
    model: str | None = None
    pinned: bool | None = None


class ChatSendInput(BaseModel):
    content: str
    item_id: str | None = None
    selection: str = ""
    account: str | None = None
    temperature: float = 0.5
    web_search: bool = False


class AnalysisEditInput(BaseModel):
    path: str
    value: str


class CollectionInput(BaseModel):
    name: str
    scope: str = "library"


class CollectionPatch(BaseModel):
    name: str


class CollectionEntryInput(BaseModel):
    collection_id: str
    entry_type: str
    entry_id: str
    member: bool | None = None


class ItemDeleteInput(BaseModel):
    ids: list[str] = Field(default_factory=list)
    note: str = ""


class FilterRuleInput(BaseModel):
    scope: str = "account"
    account: str = ""
    field: str = "title"
    patterns: str = ""
    match_mode: str = "any"
    note: str = ""
    apply_now: bool = True


class FilterRulePatch(BaseModel):
    enabled: bool = True


def _source_view(row: dict[str, Any]) -> dict[str, Any]:
    item = row_with_json(row, "config_json", "cursor_json")
    if item["type"] == "wechat_public_local":
        cursor = item.get("cursor") or {}
        item["local_catalog"] = {
            "matched": int(cursor.get("catalog_matched_accounts", 0) or 0),
            "unmatched": int(cursor.get("catalog_unmatched_names", 0) or 0),
        }
    item.pop("cursor", None)
    item["capability"] = {
        "rss": "持续监测 RSS/Atom；公众号或UP主能力取决于所配置的订阅源",
        "wechat_datax": "第三方公众号文章列表；需准确账号清单和付费 Key；不读取个人关注列表",
        "jsonl": "本地分享桥实时尾随；只读取明确配置的文件",
        "bilibili_datax": "第三方B站关键词/UP主监测；按次计费；公开视频归档不依赖此接口",
        "wecom_kf": "企业微信微信客服：接收微信内转发到客服会话的文章链接，可回读最近3天消息",
        "wechat_local": "只读当前微信3.x指定聊天窗口（含WAL）；历史链接仅建索引，新转发自动归档与精读",
        "wechat_public_local": "只读当前微信3.x PublicMsg；默认联动公众号库启用名单，本地增量追新并过滤非科研内容", 
    }.get(item["type"], "自定义采集器")
    if item["type"] in {"wechat_datax", "wechat_public_local"}:
        catalog = db.one("SELECT COUNT(*) total,SUM(enabled) enabled FROM wechat_accounts")
        item["catalog_total"] = catalog["total"] if catalog else 0
        item["catalog_enabled"] = catalog["enabled"] if catalog else 0
        if item["type"] == "wechat_public_local":
            item["catalog_linked"] = bool(item.get("config", {}).get("use_catalog", True))
    return item


@app.get("/")
def index() -> HTMLResponse:
    # Version the asset URLs so a cached page picks up new JS/CSS on the next load.
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name in _ASSET_FILES:
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={_asset_version(name)}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/_search_probe")
async def search_probe(q: str, limit: int = 6) -> dict[str, Any]:
    """临时诊断入口：直接看检索聚合结果，不调用模型。"""
    from .services.websearch import gather
    return await gather(q, limit=limit)


@app.get("/api/health")
def health() -> dict[str, Any]:
    stats = db.stats()
    heartbeat = (stats.get("worker_heartbeat") or {}).get("value")
    alive = False
    if heartbeat:
        from .utils import parse_time, utcnow
        alive = bool(parse_time(heartbeat) and (utcnow() - parse_time(heartbeat)).total_seconds() < 15)
    return {"status": "ok", "version": __version__, "worker_alive": alive, "time": now_iso(), **stats}


@app.get("/api/progress")
def progress() -> dict[str, Any]:
    """Live pipeline progress so the page can show a real bar instead of guessing."""
    from datetime import datetime, timedelta, timezone
    counts = db.one("""SELECT COUNT(*) total,
        SUM(status='discovered') discovered, SUM(status='archived') archived,
        SUM(status='ready') ready, SUM(status='filtered') filtered, SUM(status='error') errors,
        SUM(status='discovered' AND COALESCE(json_extract(metadata_json,'$.processing_policy'),'')='history_index_only') index_only
        FROM items WHERE excluded_at IS NULL""") or {}
    queued = db.one("""SELECT
        SUM(kind='archive_item' AND state IN ('READY','RETRY_WAIT')) archive_queued,
        SUM(kind='archive_item' AND state='LEASED') archive_running,
        SUM(kind='analyze_item' AND state IN ('READY','RETRY_WAIT')) analyze_queued,
        SUM(kind='analyze_item' AND state='LEASED') analyze_running
        FROM jobs WHERE state IN ('READY','RETRY_WAIT','LEASED')""") or {}
    running = []
    for row in db.query("""SELECT kind,payload_json,updated_at FROM jobs
        WHERE state='LEASED' AND kind IN ('archive_item','analyze_item','archive_history_batch','papers_item')
        ORDER BY updated_at"""):
        payload = json_loads(row["payload_json"], {}) or {}
        title = ""
        if payload.get("item_id"):
            found = db.one("SELECT title FROM items WHERE id=?", (payload["item_id"],))
            title = (found or {}).get("title") or ""
        started = parse_time(row["updated_at"])
        running.append({"kind": row["kind"], "title": title[:80],
                        "elapsed_seconds": int((datetime.now(timezone.utc) - started).total_seconds()) if started else None})
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="milliseconds")
    recent = db.one("""SELECT SUM(kind='archive_item') archives, SUM(kind='analyze_item') analyses
        FROM jobs WHERE state='SUCCEEDED' AND updated_at>=?""", (cutoff,)) or {}
    archives_per_min = round(float(recent.get("archives") or 0) / 10, 2)
    archive_queued = int(queued.get("archive_queued") or 0)
    eta = None
    if archive_queued:
        rate = archives_per_min or (0.6 * max(1, int(queued.get("archive_running") or 0)))
        eta = int(archive_queued / rate * 60) if rate else None
    today_calls = db.one("SELECT value FROM runtime WHERE key=?", ("llm_calls:" + local_date(),))
    # 批次是一条链：每批跑完再排下一批，两批之间有一两秒空档。只看「正在执行」会让
    # 停止按钮一闪一闪，所以按「还有没有未终结的批次任务」来判断。
    batch_jobs = db.one("""SELECT COUNT(*) c FROM jobs WHERE kind='archive_history_batch'
        AND state IN ('READY','RETRY_WAIT','LEASED')""")
    return {
        "total": counts.get("total") or 0, "discovered": counts.get("discovered") or 0,
        "index_only": counts.get("index_only") or 0, "archived": counts.get("archived") or 0,
        "ready": counts.get("ready") or 0, "filtered": counts.get("filtered") or 0, "errors": counts.get("errors") or 0,
        "archive_queued": archive_queued, "archive_running": int(queued.get("archive_running") or 0),
        "analyze_queued": int(queued.get("analyze_queued") or 0), "analyze_running": int(queued.get("analyze_running") or 0),
        "archives_last_10min": int(recent.get("archives") or 0), "analyses_last_10min": int(recent.get("analyses") or 0),
        "archives_per_minute": archives_per_min, "eta_seconds": eta, "running": running,
        "archives_batch_running": sum(1 for j in running if j["kind"] == "archive_history_batch"),
        "archive_batch_jobs": int((batch_jobs or {}).get("c") or 0),
        "archive_paused": bool(db.get_runtime(ARCHIVE_PAUSE_KEY)),
        "llm_calls_today": int((today_calls or {}).get("value") or 0),
        "llm_call_limit": int((get_settings().get("llm") or {}).get("daily_call_limit") or 0),
        "time": now_iso(),
    }


@app.post("/api/ingest")
def ingest(data: IngestInput) -> dict[str, Any]:
    urls = extract_urls(data.text)
    if not urls:
        raise HTTPException(400, "没有识别到 HTTP/HTTPS 链接")
    results = []
    for url in urls[:20]:
        kind = "video" if data.kind == "video" or (data.kind == "auto" and (extract_bvid(url) or "b23.tv/" in url)) else "article"
        candidate = {"url": url, "kind": kind, "title": "", "external_key": url,
                     "metadata": {"ingest": "web_or_share", "manual_ingest": True}}
        assessment = assess_candidate(candidate)
        if not assessment["keep"]:
            results.append({"id": None, "created": False, "filtered": True, "reason": assessment["reason"], "url": url})
            continue
        item_id, created = db.discover_item(candidate)
        results.append({"id": item_id, "created": created, "filtered": False, "url": url})
    return {"items": results}


@app.post("/api/sources/wechat/import")
def import_wechat_accounts(data: WeChatImportInput) -> dict[str, Any]:
    import re
    names = []
    seen = set()
    for raw in re.split(r"[\r\n]+", data.names_text):
        name = re.sub(r"^\s*(?:[-*•]+|\d+[.、])\s*", "", raw).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    if not names:
        raise HTTPException(400, "请粘贴公众号名称，每行一个")
    if len(names) > 1000:
        raise HTTPException(400, "单次最多导入 1000 个公众号")
    existing = db.one("SELECT * FROM sources WHERE type='wechat_datax' ORDER BY created_at")
    has_key = secret_status()["datax_api_key"]
    timestamp = now_iso()
    if existing:
        source_id = existing["id"]
        config = json_loads(existing["config_json"], {}) or {}
        accounts = config.get("accounts") or []
        identities = {str(a.get("id") or a.get("name", "")).casefold() for a in accounts if isinstance(a, dict)}
        added = 0
        for name in names:
            if name.casefold() not in identities:
                accounts.append({"name": name})
                identities.add(name.casefold())
                added += 1
        config.update({"accounts": accounts, "max_price_per_call": config.get("max_price_per_call", "0.05"),
                       "initial_backfill_days": data.initial_backfill_days, "overlap_hours": config.get("overlap_hours", 24),
                       "max_pages_per_account_per_run": config.get("max_pages_per_account_per_run", 3),
                       "adaptive_polling": config.get("adaptive_polling", True)})
        enabled = bool(existing["enabled"]) or has_key
        db.execute("""UPDATE sources SET name='我的微信公众号',config_json=?,poll_seconds=?,enabled=?,
            next_poll_at=?,updated_at=? WHERE id=?""", (stable_json(config), max(30, data.poll_seconds), int(enabled),
            timestamp if enabled else None, timestamp, source_id))
    else:
        source_id = str(uuid.uuid4())
        config = {"accounts": [{"name": name} for name in names], "max_price_per_call": "0.05",
                  "initial_backfill_days": data.initial_backfill_days, "overlap_hours": 24,
                  "max_pages_per_account_per_run": 3, "adaptive_polling": True}
        added = len(names)
        enabled = has_key
        db.execute("""INSERT INTO sources(id,type,name,config_json,enabled,poll_seconds,next_poll_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (source_id, "wechat_datax", "我的微信公众号", stable_json(config), int(enabled),
            max(30, data.poll_seconds), timestamp if enabled else None, timestamp, timestamp))
    db.add_event("source_created" if not existing else "source_updated",
                 f"公众号清单导入：新增 {added} 个，待Key启用={not has_key}", source_id=source_id)
    return {"source_id": source_id, "added": added, "total": len(names), "needs_key": not has_key,
            "enabled": enabled, "initial_backfill_days": data.initial_backfill_days}


@app.get("/api/sources")
def sources() -> list[dict[str, Any]]:
    return [_source_view(row) for row in db.query("SELECT * FROM sources ORDER BY created_at")]


@app.post("/api/sources")
def create_source(data: SourceInput) -> dict[str, Any]:
    if data.type not in COLLECTORS:
        raise HTTPException(400, "不支持的采集器类型")
    if data.type == "rss" and not data.config.get("url"):
        raise HTTPException(400, "RSS 来源需要 config.url")
    if data.type == "jsonl" and not data.config.get("path"):
        raise HTTPException(400, "JSONL 来源需要 config.path")
    if data.type == "wechat_datax" and not data.config.get("accounts"):
        raise HTTPException(400, "公众号来源需要 accounts，例如 [{\"name\":\"...\",\"id\":\"gh_...\"}]")
    if data.type == "bilibili_datax" and not (data.config.get("queries") or data.config.get("up_ids")):
        raise HTTPException(400, "B站来源需要 queries 关键词或 up_ids（UP主 mid）")
    if data.type == "wecom_kf" and not (data.config.get("corp_id") and data.config.get("open_kf_id")):
        raise HTTPException(400, "微信客服来源需要 corp_id 和 open_kf_id")
    source_id = str(uuid.uuid4())
    timestamp = now_iso()
    config = dict(data.config)
    if data.type == "wechat_local":
        if config.get("history_mode", "index_only") not in {"index_only", "full"}:
            raise HTTPException(400, "wechat_local 的 history_mode 仅支持 index_only 或 full")
        config.setdefault("history_mode", "index_only")
        config.setdefault("auto_process_after", timestamp)
    if data.type == "wechat_public_local":
        config.setdefault("use_catalog", True)
    db.execute("""INSERT INTO sources(id,type,name,config_json,enabled,poll_seconds,next_poll_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)""", (source_id, data.type, data.name.strip(), stable_json(config), int(data.enabled),
        max(2, data.poll_seconds), timestamp if data.enabled else None, timestamp, timestamp))
    db.add_event("source_created", f"创建来源：{data.name}", source_id=source_id)
    return _source_view(db.one("SELECT * FROM sources WHERE id=?", (source_id,)))


@app.patch("/api/sources/{source_id}")
def update_source(source_id: str, data: SourcePatch) -> dict[str, Any]:
    source = db.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if not source:
        raise HTTPException(404, "来源不存在")
    patch = data.model_dump(exclude_none=True)
    fields, params = [], []
    for key in ("name", "poll_seconds", "enabled"):
        if key in patch:
            fields.append(f"{key}=?")
            params.append(int(patch[key]) if key in {"enabled", "poll_seconds"} else patch[key])
    if "config" in patch:
        fields.append("config_json=?")
        params.append(stable_json(patch["config"]))
    if "enabled" in patch:
        fields.append("next_poll_at=?")
        params.append(now_iso() if patch["enabled"] else None)
    if fields:
        fields.append("updated_at=?")
        params.extend([now_iso(), source_id])
        db.execute(f"UPDATE sources SET {','.join(fields)} WHERE id=?", params)
    return _source_view(db.one("SELECT * FROM sources WHERE id=?", (source_id,)))


@app.post("/api/sources/{source_id}/run")
def run_source(source_id: str) -> dict[str, Any]:
    if not db.one("SELECT id FROM sources WHERE id=?", (source_id,)):
        raise HTTPException(404, "来源不存在")
    key = f"poll:{source_id}:manual:{uuid.uuid4()}"
    db.enqueue_job("poll_source", {"version": 1, "source_id": source_id}, dedupe_key=key, priority=10)
    return {"queued": True}


@app.post("/api/sources/{source_id}/archive-history")
def archive_history(source_id: str) -> dict[str, Any]:
    source = db.one("SELECT id,type FROM sources WHERE id=?", (source_id,))
    if not source or source["type"] != "wechat_local":
        raise HTTPException(404, "本地聊天来源不存在")
    remaining = db.one("SELECT COUNT(*) c FROM items WHERE source_id=? AND status='discovered' AND excluded_at IS NULL", (source_id,))["c"]
    if not remaining:
        return {"queued": False, "remaining": 0}
    db.enqueue_job("archive_history_batch", {"version": 1, "source_id": source_id, "batch_size": 8},
                   dedupe_key=f"history-archive-start:{source_id}:{uuid.uuid4()}", priority=200, max_attempts=3)
    return {"queued": True, "remaining": remaining, "llm_calls": 0}


@app.post("/api/archive-index-only")
def archive_index_only() -> dict[str, Any]:
    """Queue throttled full-text archiving for every index-only item (no cloud reading)."""
    from .pipeline import start_archive_batch
    return start_archive_batch(db)


@app.post("/api/archive-index-only/stop")
def archive_index_only_stop() -> dict[str, Any]:
    """叫停批量归档：撤销排队批次，正在跑的那一批做完当前条目就停下。"""
    from .pipeline import stop_archive_batch
    return stop_archive_batch(db)


@app.get("/api/wechat/accounts")
def wechat_accounts(q: str = "", category: str = "", fit: str = "", enabled: str = "",
                    limit: int = 2000, offset: int = 0) -> dict[str, Any]:
    conditions, params = [], []
    if q.strip():
        conditions.append("(name LIKE ? OR description LIKE ? OR reason LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like])
    if category:
        conditions.append("category_code=?")
        params.append(category)
    if fit:
        conditions.append("fit=?")
        params.append(fit)
    if enabled in {"0", "1"}:
        conditions.append("enabled=?")
        params.append(int(enabled))
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    total = db.one(f"SELECT COUNT(*) c FROM wechat_accounts {where}", params)["c"]
    rows = db.query(f"SELECT * FROM wechat_accounts {where} ORDER BY enabled DESC,category_code,name LIMIT ? OFFSET ?",
                    [*params, min(2000, max(1, limit)), max(0, offset)])
    # 最近文章来自实际收录的条目，而不是建库时的静态简介。
    recent_by_author: dict[str, dict[str, Any]] = {}
    recent_by_account: dict[str, dict[str, Any]] = {}
    for row in db.query("""SELECT author,title,discovered_at,
            json_extract(metadata_json,'$.wechat_account_id') AS account_id
        FROM items WHERE kind='article' AND excluded_at IS NULL
        ORDER BY discovered_at DESC LIMIT 6000"""):
        record = {"title": row["title"], "discovered_at": row["discovered_at"]}
        author = (row["author"] or "").strip()
        if author and author not in recent_by_author:
            recent_by_author[author] = record
        account_id = (row["account_id"] or "").strip()
        if account_id and account_id not in recent_by_account:
            recent_by_account[account_id] = record
    for row in rows:
        row["recent_article"] = (recent_by_author.get(row["name"])
                                 or recent_by_account.get((row["account_id"] or "").strip()))
    stats = db.one("""SELECT COUNT(*) total,SUM(enabled) enabled,
        SUM(CASE WHEN fit='CORE' THEN 1 ELSE 0 END) core,
        SUM(CASE WHEN fit='OPTIONAL' THEN 1 ELSE 0 END) optional,
        SUM(CASE WHEN fit='EXCLUDE' THEN 1 ELSE 0 END) excluded
        FROM wechat_accounts""")
    return {"total": total, "items": rows, "stats": stats or {}}


@app.post("/api/wechat/accounts/sync-local")
def sync_local_wechat_accounts() -> dict[str, Any]:
    """把本机微信里「我关注的全部公众号」灌进公众号库，默认一个都不追踪。

    4.x 走 wechat4_live（库在 xwechat_files、按会话分表）；读不到就回退到 3.x 的
    Contact 表。新装环境必须走这一步：先让用户看见自己关注了什么，再由他自己勾选——
    自动按分类勾选会把资料库变成垃圾场，所以 enabled 一律 0。
    """
    from .services import wechat4_live, wechat_panel, wechat_self_archive

    rows: list[dict[str, Any]] = []
    channel = ""
    detail: dict[str, Any] = {}
    try:
        state = wechat4_live.refresh()
        rows = wechat4_live.account_rows()
        if rows:
            channel = "4.x"
            detail = {"storage": str(state.storage), "refreshed": state.refreshed}
    except Exception:                                # noqa: BLE001 - 没有 4.x 就走 3.x
        rows = []
    if not rows:
        try:
            _, root, key = wechat_self_archive._account_and_key()
            directory = wechat_self_archive._official_account_directory(root, key)
        except Exception as exc:                     # noqa: BLE001
            raise HTTPException(400, f"读取本机微信公众号失败：{exc}") from exc
        rows = [{"name": info["name"], "account_id": user, "description": ""}
                for user, info in directory.items()]
        channel = "3.x"
    if not rows:
        raise HTTPException(
            400,
            "本机微信里没读到公众号。请确认微信已登录并保持运行，"
            "且在微信里打开过一次「订阅号消息」后重试。")
    result = wechat_panel.sync_account_rows(db, rows, channel)
    return {"ok": True, "channel": channel, "found": len(rows), **result, **detail}


@app.patch("/api/wechat/accounts/{account_pk}")
def update_wechat_account(account_pk: int, data: WeChatAccountPatch) -> dict[str, Any]:
    row = db.one("SELECT * FROM wechat_accounts WHERE id=?", (account_pk,))
    if not row:
        raise HTTPException(404, "公众号不存在")
    changes, params = [], []
    if data.enabled is not None:
        changes.append("enabled=?")
        params.append(int(data.enabled))
    if data.fit is not None:
        fit = str(data.fit).strip().upper()
        if fit not in {"CORE", "OPTIONAL", "EXCLUDE"}:
            raise HTTPException(400, "建议只能是 CORE / OPTIONAL / EXCLUDE")
        changes.append("fit=?")
        params.append(fit)
    if data.category_code is not None:
        changes.append("category_code=?")
        params.append(str(data.category_code).strip() or "OTHER")
    if not changes:
        return {"updated": 0}
    db.execute(f"UPDATE wechat_accounts SET {','.join(changes)},updated_at=? WHERE id=?",
               [*params, now_iso(), account_pk])
    if data.enabled is False:
        db.add_event("wechat_account_disabled", f"停用公众号：{row['name']}")
    return {"updated": 1, "enabled": row.get("enabled") if data.enabled is None else int(data.enabled),
            "fit": data.fit or row.get("fit"), "category_code": data.category_code or row.get("category_code")}


@app.get("/api/wechat/accounts/{account_pk}/articles")
def wechat_account_articles(account_pk: int, limit: int = 24, offset: int = 0, days: int = 365) -> dict[str, Any]:
    """某个号最近的推文，带上「在资料库里是什么状态」。

    数据直接来自本机微信的公众号消息库（只读、零费用），不是本站收录的内容，
    所以能看到很多还没进雷达的推文——那正是「补录」的来源。
    """
    account = db.one("SELECT * FROM wechat_accounts WHERE id=?", (account_pk,))
    if not account:
        raise HTTPException(404, "公众号不存在")
    from .services import filters, wechat4_live, wechat_self_archive
    channel = wechat_self_archive.local_channel()
    gh_id = (account.get("account_id") or "").strip()
    if channel == "4.x" and not gh_id:
        # 4.x 的号名要从它自己的通讯录里对，3.x 那份是冻结的旧库
        gh_id = wechat4_live.gh_for_name(account["name"])
    if not gh_id:
        gh_id = wechat_public.gh_id_for(account["name"], gh_id)
    if gh_id and (account.get("account_id") or "") != gh_id:
        # 顺手把编号补上：本机监测和面板都靠它对号。
        db.execute("UPDATE wechat_accounts SET account_id=?,updated_at=? WHERE id=?",
                   (gh_id, now_iso(), account_pk))
        account["account_id"] = gh_id
    base = {"account": {"id": account["id"], "name": account["name"], "account_id": gh_id,
                        "category_code": account.get("category_code"), "fit": account.get("fit"),
                        "enabled": int(account.get("enabled") or 0)},
            "total": 0, "offset": offset, "limit": limit, "has_more": False, "items": []}
    if not gh_id:
        return {**base, "error": "没能把网页上的号名对到微信内部编号：微信里可能没关注它，或者备注名和清单不一致"}
    if channel == "4.x":
        # 合并后要支持翻页：两路微信通道至少取到当前页末尾，否则第 2 页开始
        # 就只剩资料库那一路了。
        window = min(200, max(60, offset + limit))
        rows = wechat4_live.recent_for(gh_id, limit=window, days=days)
        # 4.x 客户端不落盘公众号正文，升级前的历史全在 3.x 的 PublicMsg.db 里。
        # 两路拼起来才是一个号完整的样子；否则面板上永远是"只有一两条"。
        try:
            from .services import wechat3_public
            legacy = wechat3_public.recent_for(gh_id, limit=window, days=days)
        except Exception:                               # noqa: BLE001
            legacy = []
        # 第三路是资料库自己：4.x 不落盘、3.x 库冻结、内存盒子只留最近几百条，
        # 只问微信的话列表会停在换通道那天，而右侧小字（本站最近收录）还在往前
        # 走——11-16 之后"点了才缺一大截"就是这么来的。
        known = wechat_panel.library_articles(db, gh_id, account["name"], days=days)
        merged: dict[str, dict] = {}
        for row in list(rows) + list(legacy) + list(known):
            merged.setdefault(row["canonical_url"], row)
        rows = sorted(merged.values(), key=lambda item: item.get("create_time") or 0, reverse=True)
        hint = wechat4_live.session_hint(gh_id)
        articles = [{
            "canonical_url": row["canonical_url"],
            "url": row["url"],
            "title": row["title"] or "(无标题)",
            "summary": (row["digest"] or "")[:160],
            "cover": "",
            "published_at": datetime.fromtimestamp(row["create_time"], timezone.utc)
                                     .isoformat(timespec="seconds") if row["create_time"] else "",
            "message_id": row["local_id"],
            # 一次群发多篇：同一 message_id 下的第几篇，前端据此区分卡片
            "idx": int(row.get("idx") or 1),
        } for row in rows[offset:offset + limit]]
        payload = {"items": articles, "total": len(rows),
                   "legacy_filled": bool(legacy),
                   "has_more": len(rows) > offset + len(articles)}
        if not articles and hint:
            # 正文没落盘时，会话表里至少还有「最后一条推文的时间」，标题也常常还有
            title = (hint["title"] or "").strip()
            head = (f"微信在 {hint['at_display']} 收到过这个号的推送"
                    + (f"（「{title[:36]}」）" if title else "，标题已被清过"))
            payload["note"] = head + ("，但正文没保存到本地。4.x 客户端只在打开订阅号时"
                                      "去服务端拉一次，正文不会常驻磁盘。")
        elif hint.get("at") and rows and hint["at"] > rows[0]["create_time"] + 60:
            # 有正文，但更近的那条没存下来——同样得说清楚，别让人以为就这些
            title = (hint["title"] or "").strip()
            payload["note"] = (f"更近的一条（{hint['at_display']}"
                               + (f"「{title[:36]}」" if title else "") +
                               "）微信没把正文存下来，所以列表里没有它。")
    else:
        try:
            payload = wechat_public.recent_articles(gh_id, limit=limit, offset=offset, days=days)
        except wechat_public.PublicMsgUnavailable as exc:
            return {**base, "error": str(exc)}
    articles = payload.get("items") or []
    canonicals = [item["canonical_url"] for item in articles]
    marks = wechat_panel.marks_for(db, canonicals)
    library = wechat_panel.library_index(db, canonicals)
    rules = filters.load_rules(db)
    for item in articles:
        state, detail = wechat_panel.state_of(item["canonical_url"], library, marks)
        item["state"] = state
        item["detail"] = detail or {}
        if state == "new":
            hit = filters.match(rules, account["name"], item["title"], item["summary"])
            if hit:
                item["rule_note"] = filters.describe(hit["rule"])
    base.update({"items": articles, "total": payload.get("total") or 0,
                 "has_more": bool(payload.get("has_more")),
                 "note": payload.get("note") or "",
                 "library_count": sum(1 for item in articles if item["state"] in {"archived", "index_only"}),
                 "rules": wechat_panel.rules_for_account(db, account, gh_id)})
    return base


@app.post("/api/wechat/accounts/{account_pk}/articles/import")
def import_wechat_articles(account_pk: int, data: WeChatArticleAction) -> dict[str, Any]:
    account = db.one("SELECT * FROM wechat_accounts WHERE id=?", (account_pk,))
    if not account:
        raise HTTPException(404, "公众号不存在")
    if not data.articles:
        raise HTTPException(400, "没有选择推文")
    gh_id = wechat_public.gh_id_for(account["name"], account.get("account_id") or "")
    result = wechat_panel.import_articles(db, account, gh_id, [a.model_dump() for a in data.articles])
    db.add_event("wechat_articles_imported",
                 f"公众号面板补录「{account['name']}」{result['created']} 条，已排队归档")
    return result


@app.post("/api/wechat/accounts/{account_pk}/articles/exclude")
def exclude_wechat_articles(account_pk: int, data: WeChatExcludeInput) -> dict[str, Any]:
    account = db.one("SELECT * FROM wechat_accounts WHERE id=?", (account_pk,))
    if not account:
        raise HTTPException(404, "公众号不存在")
    if not data.articles:
        raise HTTPException(400, "没有选择推文")
    gh_id = wechat_public.gh_id_for(account["name"], account.get("account_id") or "")
    mode = data.mode if data.mode in {"one", "rule"} else "one"
    try:
        result = wechat_panel.exclude_articles(
            db, account, gh_id, [a.model_dump() for a in data.articles], mode=mode,
            reason=data.reason, keywords=data.keywords, account_match=data.account_match)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if mode == "rule" and result.get("rule_id"):
        db.execute("UPDATE filter_rules SET hits=hits+?,updated_at=? WHERE id=?",
                   (result["excluded"], now_iso(), result["rule_id"]))
    if data.disable_account:
        wechat_panel.disable_account(db, account_pk, data.reason or "面板里排除同名内容")
    db.add_event("wechat_articles_excluded",
                 f"公众号面板排除「{account['name']}」{result['excluded']} 条：{result['reason']}"
                 + (f"；已建规则 #{result['rule_id']}" if result.get("rule_id") else ""))
    return result


@app.post("/api/wechat/accounts/{account_pk}/articles/restore")
def restore_wechat_articles(account_pk: int, data: WeChatArticleAction) -> dict[str, Any]:
    if not db.one("SELECT id FROM wechat_accounts WHERE id=?", (account_pk,)):
        raise HTTPException(404, "公众号不存在")
    return wechat_panel.restore_articles(db, [a.model_dump() for a in data.articles])


@app.get("/api/wechat/exclude-presets")
def wechat_exclude_presets() -> dict[str, Any]:
    return wechat_panel.preset_payload()


@app.get("/api/wechat/space")
def wechat_space_status() -> dict[str, Any]:
    """微信把文件放哪了、C 盘被占了多少。只读。"""
    from .services import wechat_space
    return wechat_space.snapshot()


@app.post("/api/wechat/space/fix")
def wechat_space_fix() -> dict[str, Any]:
    """补上"就算配置被重置、数据也落不到 C 盘"的那条软链接。"""
    from .services import wechat_space
    return wechat_space.ensure()


@app.post("/api/wechat/space/clean")
def wechat_space_clean(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """回收微信的安装包缓存和旧日志。默认 dry-run，要先看一眼再真删。"""
    from .services import wechat_space
    dry_run = True if not data else bool(data.get("apply") is not True)
    return wechat_space.clean(dry_run=dry_run)


@app.get("/api/wechat/diagnostics")
def wechat_diagnostics(days: int = 7) -> dict[str, Any]:
    """排障：通道判定、分片读取情况、采集源最后一条错误。"""
    from .services import wechat4_live
    from .services.wechat_self_archive import local_channel

    channel = local_channel()
    payload: dict[str, Any] = {"channel": channel}
    try:
        payload["four_x"] = wechat4_live.diagnose(days=max(1, min(90, days)))
    except Exception as exc:                            # noqa: BLE001
        payload["four_x"] = {"error": f"{type(exc).__name__}: {exc}"}
    rows = db.query("SELECT id,name,type,enabled,last_success_at,last_error,cursor_json "
                    "FROM sources WHERE type IN ('wechat_public_local','wechat_local')")
    payload["sources"] = [{key: row[key] for key in
                           ("id", "name", "type", "enabled", "last_success_at", "last_error")}
                          for row in rows]
    return payload


@app.get("/api/backup")
def backup_status() -> dict[str, Any]:
    from .services import backup
    return backup.status()


@app.post("/api/backup/run")
def backup_run() -> dict[str, Any]:
    from .services import backup
    return backup.run(force=True)



@app.post("/api/wechat/accounts/bulk")
def bulk_wechat_accounts(data: WeChatAccountBulkInput) -> dict[str, Any]:
    ids = list(dict.fromkeys(int(x) for x in data.ids))
    if not ids:
        raise HTTPException(400, "没有选择公众号")
    updated = 0
    with db.transaction() as conn:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            cursor = conn.execute(f"UPDATE wechat_accounts SET enabled=?,updated_at=? WHERE id IN ({marks})",
                                  [int(data.enabled), now_iso(), *chunk])
            updated += cursor.rowcount
    return {"updated": updated, "enabled": data.enabled}


@app.post("/api/wechat/catalog/import")
def import_wechat_catalog() -> dict[str, Any]:
    source_path = DATA_DIR / "account_classification.json"
    if not source_path.is_file():
        raise HTTPException(404, "尚未生成公众号分类文件")
    accounts = json.loads(source_path.read_text(encoding="utf-8"))["accounts"]
    source = db.one("SELECT id,config_json FROM sources WHERE type='wechat_datax' ORDER BY created_at")
    known_ids = {}
    if source:
        config = json_loads(source["config_json"], {}) or {}
        for account in config.get("accounts") or []:
            if isinstance(account, dict) and account.get("id"):
                known_ids[str(account.get("name", "")).casefold()] = account["id"]
    timestamp = now_iso()
    with db.transaction() as conn:
        for account in accounts:
            suggested = int(account.get("fit") == "CORE")
            conn.execute("""INSERT INTO wechat_accounts
                (name,account_id,category_code,fit,reason,description,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET account_id=COALESCE(wechat_accounts.account_id,excluded.account_id),
                category_code=excluded.category_code,fit=excluded.fit,reason=excluded.reason,
                description=excluded.description,updated_at=excluded.updated_at""", (
                account["name"].strip(), known_ids.get(account["name"].casefold()),
                account.get("code", "OTHER"), account.get("fit", "OPTIONAL"), account.get("reason", ""),
                account.get("description", ""), suggested, timestamp, timestamp,
            ))
        if source:
            config = json_loads(source["config_json"], {}) or {}
            config["use_catalog"] = True
            config.setdefault("max_pages_per_account_per_run", 1)
            config.setdefault("max_candidates_per_account", 0)
            conn.execute("UPDATE sources SET config_json=?,updated_at=? WHERE id=?",
                         (stable_json(config), timestamp, source["id"]))
    stats = db.one("SELECT COUNT(*) total,SUM(enabled) enabled FROM wechat_accounts")
    return {"imported": len(accounts), **stats}


@app.get("/api/items")
def items(q: str = "", kind: str = "", status: str = "", limit: int = 50, offset: int = 0,
          since: str = "", until: str = "", collection: str = "",
          include_excluded: bool = False, hide_low: bool = False) -> dict[str, Any]:
    limit = min(200, max(1, limit))
    offset = max(0, offset)
    # 「低相关」是精读打分低于阈值的中间档，判断不一定准，所以默认照旧显示，
    # 只是给个开关让列表清净。真正判死的引流软文走的是 excluded_at，默认就看不见。
    hide_status = "filtered" if (hide_low and status != "filtered") else ""
    filters = {"query": q, "kind": kind, "status": status, "since": since or None,
               "until": until or None, "collection": collection or None,
               "include_excluded": include_excluded, "hide_status": hide_status}
    rows = db.search(**filters, limit=limit, offset=offset)
    total = db.search_count(**filters)
    hidden_low = 0
    if hide_status:
        hidden_low = db.search_count(query=q, kind=kind, since=since or None,
                                     until=until or None, collection=collection or None,
                                     status="filtered")
    busy: dict[str, str] = {}
    for job in db.query("""SELECT kind,state,payload_json,last_error FROM jobs
        WHERE state IN ('LEASED','READY','RETRY_WAIT') AND kind IN ('archive_item','papers_item','analyze_item')
        ORDER BY (state='LEASED') DESC, created_at"""):
        payload = json_loads(job["payload_json"], {}) or {}
        item_id = payload.get("item_id")
        if item_id and item_id not in busy:
            reason = str(job.get("last_error") or "")
            if job["kind"] == "analyze_item":
                busy[item_id] = "analyze"
            elif "人机验证" in reason or "微信验证" in reason:
                # 被微信挡住的不是「正在归档」，是「在等微信放行」——
                # 两种状态混在一起，用户只会觉得软件卡死了。
                busy[item_id] = "verify"
            else:
                busy[item_id] = "archive"
    membership = db.collection_ids_for("item", [row["id"] for row in rows])
    for row in rows:
        row["active_job"] = busy.get(row["id"])
        row["collections"] = membership.get(row["id"], [])
    return {"items": rows, "total": total, "limit": limit, "offset": offset,
            "hidden_low_relevance": hidden_low}


@app.get("/api/items/{item_id}")
def item_detail(item_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM items WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "条目不存在")
    item = row_with_json(row, "tags_json", "metadata_json", "analysis_json")
    item["collections"] = db.collection_ids_for("item", [item_id]).get(item_id, [])
    job = db.one("""SELECT kind,state,updated_at,last_error FROM jobs
        WHERE kind IN ('archive_item','papers_item','analyze_item')
        AND json_extract(payload_json,'$.item_id')=? ORDER BY updated_at DESC""", (item_id,))
    active = bool(job) and job["state"] in {"LEASED", "READY", "RETRY_WAIT"}
    reason = str((job or {}).get("last_error") or "")
    if not active:
        item["active_job"] = None
    elif job["kind"] == "analyze_item":
        item["active_job"] = "analyze"
    elif "人机验证" in reason or "微信验证" in reason:
        item["active_job"] = "verify"
    else:
        item["active_job"] = "archive"
    item["last_job"] = ({"kind": job["kind"], "state": job["state"], "updated_at": job["updated_at"],
                         "error": job["last_error"]} if job else None)
    item["papers"] = db.query("""SELECT p.*,ip.evidence FROM papers p JOIN item_papers ip ON p.id=ip.paper_id
        WHERE ip.item_id=? ORDER BY p.created_at""", (item_id,))
    item["progress"] = json_loads(db.get_runtime(f"progress:{item_id}") or "null", None)
    try:
        from .services.analyzer import figure_cards
        item["figures"] = figure_cards(item)
    except Exception:
        item["figures"] = []
    return item


@app.post("/api/items/{item_id}/verify-source")
async def verify_source(item_id: str) -> dict[str, Any]:
    """Check whether the publisher page still exists and apply the keep/drop rule."""
    from .pipeline import JobRunner
    from .services.archive import probe_source
    row = db.one("SELECT id,url,text,html_path,excluded_at FROM items WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "条目不存在")
    result = await probe_source(row["url"])
    if not result["exists"] and not row["excluded_at"]:
        JobRunner(db)._source_gone(item_id, result["reason"])
    fresh = db.one("SELECT excluded_at,exclusion_reason,html_path,metadata_json FROM items WHERE id=?", (item_id,))
    has_archive = bool(fresh and fresh["html_path"] and Path(fresh["html_path"]).is_file())
    return {"exists": result["exists"], "reason": result["reason"], "archived": has_archive,
            "kept": bool(has_archive), "excluded": bool(fresh and fresh["excluded_at"]),
            "exclusion_reason": (fresh or {}).get("exclusion_reason") if fresh else None}


@app.post("/api/items/retry-archives")
def retry_wechat_archives() -> dict[str, Any]:
    """把所有没拿到正文的公众号文章重新排一次归档。

    微信的风控是按 IP 段、按时间段放的：它放开之后，用户需要一个「重来一次」的
    按钮，而不是一条条点开详情。
    """
    rows = db.query("""SELECT id,title FROM items
        WHERE kind='article' AND excluded_at IS NULL
          AND canonical_url LIKE '%mp.weixin.qq.com%'
          AND status IN ('discovered','insufficient','error')""")
    queued = 0
    for row in rows:
        db.execute("DELETE FROM jobs WHERE kind='archive_item' AND state IN ('READY','RETRY_WAIT','DEAD')"
                   " AND json_extract(payload_json,'$.item_id')=?", (row["id"],))
        db.execute("UPDATE items SET status='discovered', error=NULL WHERE id=?", (row["id"],))
        if db.enqueue_job("archive_item", {"item_id": row["id"]}, priority=120, max_attempts=4):
            queued += 1
    if queued:
        db.add_event("wechat_archive_retry", f"手动重试公众号归档：重新排队 {queued} 条")
    return {"queued": queued, "considered": len(rows)}


@app.get("/api/items/{item_id}/archive", response_class=HTMLResponse)
def item_archive(item_id: str) -> HTMLResponse:
    row = db.one("SELECT html_path FROM items WHERE id=?", (item_id,))
    if not row or not row.get("html_path") or not Path(row["html_path"]).is_file():
        raise HTTPException(404, "离线阅读页尚未生成")
    content = Path(row["html_path"]).read_text(encoding="utf-8")
    content = content.replace("../../blobs/", "/api/blobs/")
    # The reading template changes; a cached copy would keep the old viewer forever.
    return HTMLResponse(content, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.post("/api/items/{item_id}/restore")
def restore_item(item_id: str) -> dict[str, Any]:
    row = db.one("SELECT id,excluded_at FROM items WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "条目不存在")
    if row["excluded_at"]:
        db.set_items_excluded([item_id], "手动恢复收录", excluded=False, manual=True)
        db.add_event("restored", "手动恢复收录", item_id)
    return {"restored": bool(row["excluded_at"])}


@app.post("/api/items/{item_id}/delete")
def delete_item(item_id: str, data: ItemDeleteInput = ItemDeleteInput()) -> dict[str, Any]:
    """手动删除：移出资料库但保留已归档的正文与图片，随时能恢复。"""
    row = db.one("SELECT id,excluded_at,exclusion_reason FROM items WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "条目不存在")
    if row["excluded_at"]:
        return {"deleted": False, "reason": row.get("exclusion_reason") or "已经在资料库之外"}
    reason = "手动删除"
    note = data.note.strip()
    if note:
        reason += f"：{note[:120]}"
    db.set_items_excluded([item_id], reason)
    db.add_event("item_deleted", f"手动移出资料库（{reason}）", item_id)
    return {"deleted": True, "reason": reason}


@app.post("/api/items/bulk-delete")
def delete_items(data: ItemDeleteInput) -> dict[str, Any]:
    ids = [item_id for item_id in dict.fromkeys(data.ids) if item_id]
    if not ids:
        raise HTTPException(400, "没有指定要删除的条目")
    reason = "手动删除"
    if data.note.strip():
        reason += f"：{data.note.strip()[:120]}"
    changed = db.set_items_excluded(ids, reason)
    if changed:
        db.add_event("item_deleted", f"手动移出资料库 {changed} 条（{reason}）")
    return {"deleted": changed, "reason": reason}


@app.get("/api/filter-rules")
def list_filter_rules() -> dict[str, Any]:
    from .services import filters
    rules = filters.load_rules(db, enabled_only=False)
    for rule in rules:
        rule["description"] = filters.describe(rule)
    return {"items": rules}


@app.post("/api/filter-rules/preview")
def preview_filter_rule(data: FilterRuleInput) -> dict[str, Any]:
    from .services import filters
    return filters.preview(db, data.model_dump())


@app.post("/api/filter-rules")
def create_filter_rule(data: FilterRuleInput) -> dict[str, Any]:
    from .services import filters
    patterns = filters.split_patterns(data.patterns)
    if not patterns:
        raise HTTPException(400, "至少写一个关键词")
    if data.scope not in filters.SCOPE_LABELS:
        raise HTTPException(400, "scope 只能是 account 或 all")
    if data.scope == "account" and not data.account.strip():
        raise HTTPException(400, "限定公众号时必须填公众号名称")
    if data.field not in filters.FIELD_LABELS:
        raise HTTPException(400, "field 只能是 title 或 header")
    timestamp = now_iso()
    db.execute("""INSERT INTO filter_rules(scope,account,field,patterns,match_mode,note,enabled,hits,created_at,updated_at)
        VALUES(?,?,?,?,?,?,1,0,?,?)""",
               (data.scope, data.account.strip(), data.field, "|".join(patterns),
                "all" if data.match_mode == "all" else "any", data.note.strip(), timestamp, timestamp))
    row = (db.query("SELECT * FROM filter_rules ORDER BY id DESC") or [None])[0]
    if not row:
        raise HTTPException(500, "规则写入后读不回来")
    applied = filters.apply_to_existing(db) if data.apply_now else 0
    row = db.one("SELECT * FROM filter_rules WHERE id=?", (row["id"],)) or row
    row["description"] = filters.describe({**row, "pattern_list": patterns})
    return {"rule": row, "applied": applied}


@app.patch("/api/filter-rules/{rule_id}")
def update_filter_rule(rule_id: int, data: FilterRulePatch) -> dict[str, Any]:
    if not db.one("SELECT id FROM filter_rules WHERE id=?", (rule_id,)):
        raise HTTPException(404, "规则不存在")
    db.execute("UPDATE filter_rules SET enabled=?,updated_at=? WHERE id=?",
               (int(data.enabled), now_iso(), rule_id))
    return {"updated": 1}


@app.delete("/api/filter-rules/{rule_id}")
def delete_filter_rule(rule_id: int, restore: bool = True) -> dict[str, Any]:
    from .services import filters
    if not db.one("SELECT id FROM filter_rules WHERE id=?", (rule_id,)):
        raise HTTPException(404, "规则不存在")
    restored = filters.restore_by_rule(db, rule_id) if restore else 0
    db.execute("DELETE FROM filter_rules WHERE id=?", (rule_id,))
    db.add_event("filter_rule", f"删除过滤规则 #{rule_id}" + (f"，同时恢复 {restored} 条" if restored else ""))
    return {"deleted": 1, "restored": restored}


@app.get("/api/items/{item_id}/raw")
def item_raw(item_id: str) -> FileResponse:
    row = db.one("SELECT raw_path FROM items WHERE id=? AND excluded_at IS NULL", (item_id,))
    if not row or not row.get("raw_path") or not Path(row["raw_path"]).is_file():
        raise HTTPException(404, "原始快照不存在")
    return FileResponse(row["raw_path"], media_type="application/octet-stream",
                        filename=Path(row["raw_path"]).name, content_disposition_type="attachment")


@app.get("/api/blobs/{folder}/{name}")
def blob(folder: str, name: str) -> FileResponse:
    path = (BLOB_DIR / folder / name).resolve()
    if BLOB_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@app.get("/api/wechat/cover")
async def wechat_cover(u: str) -> Response:
    """代理公众号封面图，并留一份在本机。

    图片床 mmbiz.qpic.cn 有防盗链：浏览器从 127.0.0.1 直连会被换成一张
    「此图片来自微信公众平台，未经允许不可引用」的占位图，看着像图全挂了。
    服务端不带 Referer 去取是正常的，所以这里绕一手，并缓存到 blobs/covers，
    下次翻面板既不求人也不卡。
    """
    from urllib.parse import unquote, urlsplit
    target = unquote(u or "").strip()
    parts = urlsplit(target)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not (host == "qpic.cn" or host.endswith(".qpic.cn")):
        raise HTTPException(400, "只允许公众号图片床的地址")
    import hashlib
    folder = BLOB_DIR / "covers"
    folder.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha1(target.encode("utf-8")).hexdigest()
    cache_path = folder / f"{stem}.img"
    type_path = folder / f"{stem}.type"
    headers = {"Cache-Control": "public, max-age=2592000"}
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        cached_type = type_path.read_text(encoding="ascii").strip() if type_path.is_file() else "image/jpeg"
        return FileResponse(cache_path, media_type=cached_type, headers=headers)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(target, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*"})
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
        if resp.status_code != 200 or not content_type.startswith("image/") or not resp.content:
            raise HTTPException(404, "封面取不到")
        if len(resp.content) > 8 * 1024 * 1024:
            raise HTTPException(413, "封面图过大")
        cache_path.write_bytes(resp.content)
        type_path.write_text(content_type, encoding="ascii")
    except HTTPException:
        raise
    except Exception as exc:                                   # noqa: BLE001
        raise HTTPException(404, f"封面取不到：{exc}") from exc
    return FileResponse(cache_path, media_type=content_type, headers=headers)


@app.post("/api/items/{item_id}/process")
def process_discovered_item(item_id: str) -> dict[str, Any]:
    item = db.one("SELECT id,status FROM items WHERE id=? AND excluded_at IS NULL", (item_id,))
    if not item:
        raise HTTPException(404, "条目不存在")
    if item["status"] != "discovered":
        return {"queued": False, "reason": "该条目已进入或完成处理流程"}
    db.enqueue_job("archive_item", {"version": 1, "item_id": item_id},
                   dedupe_key=f"archive:{item_id}:initial", priority=50)
    return {"queued": True}


@app.post("/api/items/{item_id}/reanalyze")
def reanalyze(item_id: str) -> dict[str, Any]:
    if not db.one("SELECT id FROM items WHERE id=? AND excluded_at IS NULL", (item_id,)):
        raise HTTPException(404, "条目不存在或已移出主资料库")
    running = db.one("""SELECT id FROM jobs WHERE state IN ('READY','RETRY_WAIT','LEASED') AND kind='analyze_item'
        AND json_extract(payload_json,'$.item_id')=?""", (item_id,))
    if running:
        return {"queued": True, "already_running": True}
    key = f"analyze:{item_id}:manual:{uuid.uuid4()}"
    db.enqueue_job("analyze_item", {"version": 1, "item_id": item_id, "force": True}, dedupe_key=key, priority=15, max_attempts=4)
    return {"queued": True}


@app.post("/api/items/{item_id}/retriage-images")
async def retriage_images(item_id: str) -> dict[str, Any]:
    """Re-judge the archived images of one article, dropping ads and group photos.

    Synchronous on purpose: the user clicked it and is looking at the page, and one
    contact-sheet call is fast enough to answer with the real number.
    """
    if not db.one("SELECT id FROM items WHERE id=? AND excluded_at IS NULL", (item_id,)):
        raise HTTPException(404, "条目不存在或已移出主资料库")
    from .services.analyzer import triage_archived_images
    dropped = await triage_archived_images(db, item_id, force=True)
    return {"dropped": int(dropped), "checked": True}


@app.post("/api/diagnostics/gone-detection")
async def gone_detection() -> dict[str, Any]:
    """Prove the deleted-source detector works using links already in the library.

    Nobody can produce a deleted WeChat article on demand, so the self-check reuses
    one article this radar already found deleted, one that is still live, and an
    address that has to 404.  One click answers "does this thing actually work".
    """
    from .services.archive import probe_source
    samples: list[dict[str, str]] = []
    dead_rows = db.query("""SELECT url,title FROM items WHERE excluded_at IS NOT NULL
        AND exclusion_reason LIKE '%原文已被删除%' ORDER BY excluded_at DESC""")
    if dead_rows:
        dead = dead_rows[0]
        samples.append({"name": "已被发布者删除的公众号文章", "url": dead["url"],
                        "expect": "should_not_exist", "title": dead["title"] or ""})
    live_rows = db.query("""SELECT url,title FROM items WHERE url LIKE '%mp.weixin.qq.com%'
        AND excluded_at IS NULL ORDER BY analyzed_at DESC""")
    if live_rows:
        live = live_rows[0]
        samples.append({"name": "正常可访问的公众号文章", "url": live["url"],
                        "expect": "should_exist", "title": live["title"] or ""})
    samples.append({"name": "故意构造的不存在地址（404）",
                    "url": "https://example.com/hermes-no-such-page-404",
                    "expect": "should_not_exist", "title": ""})
    results: list[dict[str, Any]] = []
    for sample in samples:
        try:
            probe = await probe_source(sample["url"])
            expected_gone = sample["expect"] == "should_not_exist"
            passed = (not probe["exists"]) if expected_gone else bool(probe["exists"])
            results.append({**sample, "exists": probe["exists"], "reason": probe["reason"], "pass": passed})
        except Exception as exc:  # noqa: BLE001 - a network failure is itself a result
            results.append({**sample, "error": str(exc), "pass": False})
    return {"samples": results, "all_pass": all(item.get("pass") for item in results)}


@app.get("/api/papers")
def papers(status: str = "", limit: int = 0, offset: int = 0) -> Any:
    if status == "needed":
        condition = "WHERE p.status IN ('closed','failed','not_found')"
        params: tuple = ()
    elif status:
        condition = "WHERE p.status=?"
        params = (status,)
    else:
        condition, params = "", ()
    select = f"""SELECT p.*,(SELECT i.title FROM items i JOIN item_papers ip ON i.id=ip.item_id
        WHERE ip.paper_id=p.id LIMIT 1) source_title,(SELECT i.url FROM items i JOIN item_papers ip ON i.id=ip.item_id
        WHERE ip.paper_id=p.id LIMIT 1) source_url FROM papers p {condition} ORDER BY p.created_at DESC"""
    if limit <= 0:
        # 不传分页就是整份清单：CSV 导出和「复制代下清单」要的是全部。
        return db.query(select, params)
    limit = min(200, max(1, limit))
    total = int((db.one(f"SELECT COUNT(*) n FROM papers p {condition}", params) or {"n": 0})["n"])
    rows = db.query(select + " LIMIT ? OFFSET ?", params + (limit, max(0, offset)))
    return {"items": rows, "total": total, "limit": limit, "offset": max(0, offset)}


def _paper_path(paper_id: str) -> tuple[dict[str, Any], Path]:
    paper = db.one("SELECT local_path,title FROM papers WHERE id=?", (paper_id,))
    if not paper or not paper.get("local_path") or not Path(paper["local_path"]).is_file():
        raise HTTPException(404, "本地 PDF 不存在")
    return paper, Path(paper["local_path"]).resolve()


def _wps_executable() -> Path | None:
    bases = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Kingsoft" / "WPS Office",
        Path("D:/program/WPS Office"),
        Path("C:/Program Files/Kingsoft/WPS Office"),
        Path("C:/Program Files (x86)/Kingsoft/WPS Office"),
    ]
    candidates = []
    for base in bases:
        if base.is_dir():
            candidates.extend(path for path in base.glob("*/office6/wps.exe") if path.is_file())
    if candidates:
        return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return None


@app.get("/api/papers/{paper_id}/file")
def paper_file(paper_id: str, download: bool = False) -> FileResponse:
    paper, path = _paper_path(paper_id)
    return FileResponse(str(path), media_type="application/pdf",
                        filename=(paper["title"] or paper_id) + ".pdf",
                        content_disposition_type="attachment" if download else "inline")


@app.post("/api/papers/{paper_id}/open-wps")
def open_paper_wps(paper_id: str) -> dict[str, Any]:
    _, path = _paper_path(paper_id)
    wps = _wps_executable()
    if wps:
        subprocess.Popen([str(wps), str(path)], close_fds=True)
        application = "WPS"
    else:
        os.startfile(str(path))
        application = "系统默认 PDF 应用"
    return {"opened": True, "application": application, "path": str(path), "directory": str(path.parent)}


@app.post("/api/papers/{paper_id}/reveal")
def reveal_paper(paper_id: str) -> dict[str, Any]:
    _, path = _paper_path(paper_id)
    subprocess.Popen(["explorer.exe", "/select,", str(path)], close_fds=True)
    return {"opened": True, "path": str(path), "directory": str(path.parent)}


@app.get("/api/papers/needed.csv")
def papers_csv() -> Response:
    rows = papers("needed")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["题名", "作者", "年份", "期刊", "DOI", "DOI链接", "来源文章", "来源链接", "未下载原因"])
    def cell(value: Any) -> str:
        text = str(value or "")
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
    for paper in rows:
        writer.writerow([cell(value) for value in [paper["title"], "; ".join(json_loads(paper["authors_json"], []) or []), paper["year"],
                         paper["journal"], paper["doi"], f"https://doi.org/{paper['doi']}", paper.get("source_title"),
                         paper.get("source_url"), paper.get("error")]])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=papers-needed.csv"})


@app.post("/api/papers/{paper_id}/retry")
def retry_paper(paper_id: str) -> dict[str, Any]:
    link = db.one("SELECT item_id FROM item_papers WHERE paper_id=?", (paper_id,))
    if not link:
        raise HTTPException(404, "论文没有关联条目")
    db.execute("UPDATE papers SET status='discovered',error=NULL WHERE id=?", (paper_id,))
    db.enqueue_job("papers_item", {"version": 1, "item_id": link["item_id"]},
                   dedupe_key=f"papers:{link['item_id']}:retry:{uuid.uuid4()}", priority=20, max_attempts=4)
    return {"queued": True}


@app.get("/api/ideas")
def ideas(since: str = "", collection: str = "") -> list[dict[str, Any]]:
    conditions, params = [], []
    if since:
        conditions.append("updated_at>=?")
        params.append(since)
    if collection == "__none__":
        conditions.append("""NOT EXISTS (SELECT 1 FROM collection_entries ce
            WHERE ce.entry_type='idea' AND ce.entry_id=ideas.id)""")
    elif collection:
        conditions.append("""id IN (SELECT entry_id FROM collection_entries
            WHERE collection_id=? AND entry_type='idea')""")
        params.append(collection)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = [row_with_json(x, "tags_json") for x in db.query(f"SELECT * FROM ideas {where} ORDER BY updated_at DESC", params)]
    membership = db.collection_ids_for("idea", [row["id"] for row in rows])
    for row in rows:
        row["collections"] = membership.get(row["id"], [])
    return rows


@app.get("/api/collections")
def collections(scope: str = "") -> list[dict[str, Any]]:
    return db.collections(scope)


@app.post("/api/collections")
def create_collection(data: CollectionInput) -> dict[str, Any]:
    try:
        return db.collection_create(data.name, data.scope or "library")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/collections/{collection_id}")
def rename_collection(collection_id: str, data: CollectionPatch) -> dict[str, Any]:
    try:
        db.collection_rename(collection_id, data.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"updated": 1}


@app.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: str) -> dict[str, Any]:
    return {"deleted": db.collection_delete(collection_id)}


@app.get("/api/collections/{collection_id}/entries")
def collection_entries(collection_id: str, entry_type: str = "item") -> list[str]:
    return db.collection_entry_ids(collection_id, entry_type)


@app.post("/api/collections/toggle")
def toggle_collection_entry(data: CollectionEntryInput) -> dict[str, Any]:
    if data.entry_type not in {"item", "note", "idea"}:
        raise HTTPException(400, "不支持的收藏类型")
    try:
        return db.collection_toggle(data.collection_id, data.entry_type, data.entry_id, data.member)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/ideas")
def create_idea(data: IdeaInput) -> dict[str, Any]:
    idea_id = str(uuid.uuid4())
    timestamp = now_iso()
    db.execute("INSERT INTO ideas(id,title,content,tags_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
               (idea_id, data.title.strip(), data.content, stable_json(data.tags), int(data.enabled), timestamp, timestamp))
    return db.one("SELECT * FROM ideas WHERE id=?", (idea_id,))


@app.patch("/api/ideas/{idea_id}")
def update_idea(idea_id: str, data: IdeaInput) -> dict[str, Any]:
    if not db.one("SELECT id FROM ideas WHERE id=?", (idea_id,)):
        raise HTTPException(404, "idea 不存在")
    db.execute("""UPDATE ideas SET title=?,content=?,tags_json=?,enabled=?,revision=revision+1,updated_at=? WHERE id=?""",
               (data.title.strip(), data.content, stable_json(data.tags), int(data.enabled), now_iso(), idea_id))
    return db.one("SELECT * FROM ideas WHERE id=?", (idea_id,))


@app.delete("/api/ideas/{idea_id}")
def delete_idea(idea_id: str) -> dict[str, Any]:
    if not db.one("SELECT id FROM ideas WHERE id=?", (idea_id,)):
        raise HTTPException(404, "idea 不存在")
    db.execute("DELETE FROM ideas WHERE id=?", (idea_id,))
    return {"deleted": True, "id": idea_id}


@app.get("/api/credits")
def credits(days: int = 30) -> dict[str, Any]:
    from .services.credits import recent_calls, summary
    data = summary(days=min(365, max(1, days)))
    data["recent"] = recent_calls(30)
    return data


@app.get("/api/notes")
def notes(item_id: str = "", limit: int = 200, since: str = "", collection: str = "") -> list[dict[str, Any]]:
    from .services.assistant import notes_view
    limit = min(500, max(1, limit))
    conditions, params = [], []
    if item_id:
        conditions.append("n.item_id=?")
        params.append(item_id)
    if since:
        conditions.append("n.updated_at>=?")
        params.append(since)
    if collection == "__none__":
        conditions.append("""NOT EXISTS (SELECT 1 FROM collection_entries ce
            WHERE ce.entry_type='note' AND ce.entry_id=n.id)""")
    elif collection:
        conditions.append("""n.id IN (SELECT entry_id FROM collection_entries
            WHERE collection_id=? AND entry_type='note')""")
        params.append(collection)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = db.query(f"""SELECT n.*,i.title item_title,i.url item_url FROM notes n LEFT JOIN items i ON i.id=n.item_id
        {where} ORDER BY n.pinned DESC,n.created_at DESC LIMIT ?""", [*params, limit])
    result = notes_view(rows)
    membership = db.collection_ids_for("note", [row["id"] for row in result])
    for row in result:
        row["collections"] = membership.get(row["id"], [])
    return result


@app.post("/api/notes")
def create_note(data: NoteInput) -> dict[str, Any]:
    from .services.assistant import notes_view
    note_id = str(uuid.uuid4())
    timestamp = now_iso()
    db.execute("""INSERT INTO notes(id,item_id,section,quote,body,tags_json,pinned,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)""", (note_id, data.item_id, data.section[:80], data.quote[:8000],
                                        data.body[:20000], stable_json(data.tags), int(data.pinned),
                                        timestamp, timestamp))
    rows = db.query("""SELECT n.*,i.title item_title,i.url item_url FROM notes n LEFT JOIN items i ON i.id=n.item_id
        WHERE n.id=?""", (note_id,))
    return notes_view(rows)[0]


@app.patch("/api/notes/{note_id}")
def update_note(note_id: str, data: NotePatch) -> dict[str, Any]:
    from .services.assistant import notes_view
    row = db.one("SELECT * FROM notes WHERE id=?", (note_id,))
    if not row:
        raise HTTPException(404, "笔记不存在")
    body = data.body if data.body is not None else row["body"]
    tags = stable_json(data.tags) if data.tags is not None else row["tags_json"]
    pinned = int(data.pinned) if data.pinned is not None else row["pinned"]
    db.execute("UPDATE notes SET body=?,tags_json=?,pinned=?,updated_at=? WHERE id=?",
               (body[:20000], tags, pinned, now_iso(), note_id))
    rows = db.query("""SELECT n.*,i.title item_title,i.url item_url FROM notes n LEFT JOIN items i ON i.id=n.item_id
        WHERE n.id=?""", (note_id,))
    return notes_view(rows)[0]


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str) -> dict[str, Any]:
    if not db.one("SELECT id FROM notes WHERE id=?", (note_id,)):
        raise HTTPException(404, "笔记不存在")
    db.execute("DELETE FROM notes WHERE id=?", (note_id,))
    return {"deleted": True, "id": note_id}


ALLOWED_EDIT_PATHS = {"hook", "plain_problem", "obstacle", "core_idea", "answer", "summary", "one_sentence"}


def _edit_analysis(item_id: str, path: str, value: str) -> dict[str, Any]:
    """Let the reader fix wording in place without re-running the cloud model."""
    row = db.one("SELECT analysis_json FROM items WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "条目不存在")
    analysis = json_loads(row.get("analysis_json"), {}) or {}
    parts = path.split(".")
    if parts[0] == "steps" and len(parts) == 3:
        index = int(parts[1])
        steps = analysis.get("steps") or []
        if not 0 <= index < len(steps) or parts[2] not in {"title", "why", "did", "saw", "means"}:
            raise HTTPException(400, "不支持该步骤字段")
        steps[index][parts[2]] = value[:4000]
    elif parts[0] == "numbers" and len(parts) == 3:
        index = int(parts[1])
        numbers = analysis.get("numbers") or []
        if not 0 <= index < len(numbers) or parts[2] not in {"value", "meaning"}:
            raise HTTPException(400, "不支持该数字字段")
        numbers[index][parts[2]] = value[:1000]
    elif parts[0] == "terms" and len(parts) == 3:
        index = int(parts[1])
        terms = analysis.get("terms") or []
        if not 0 <= index < len(terms) or parts[2] not in {"term", "plain"}:
            raise HTTPException(400, "不支持该名词字段")
        terms[index][parts[2]] = value[:1000]
    elif path in ALLOWED_EDIT_PATHS:
        analysis[path] = value[:6000]
    else:
        raise HTTPException(400, "不支持编辑该字段")
    analysis.setdefault("manual_edits", {})
    analysis["manual_edits"][path] = now_iso()
    db.execute("UPDATE items SET analysis_json=?,summary=? WHERE id=?",
               (stable_json(analysis), analysis.get("hook") or analysis.get("summary") or "", item_id))
    return {"ok": True, "path": path, "analysis": analysis}


@app.patch("/api/items/{item_id}/analysis")
def edit_analysis(item_id: str, data: AnalysisEditInput) -> dict[str, Any]:
    return _edit_analysis(item_id, data.path, data.value)


@app.get("/api/llm/accounts")
def llm_accounts() -> dict[str, Any]:
    from .services.llm_gateway import accounts
    return {"accounts": accounts(), "default": get_settings()["llm"].get("model")}


@app.get("/api/chats")
def chats(limit: int = 60) -> list[dict[str, Any]]:
    return db.query("""SELECT c.*,COUNT(m.id) message_count FROM chats c
        LEFT JOIN chat_messages m ON m.chat_id=c.id GROUP BY c.id
        ORDER BY c.pinned DESC,c.updated_at DESC LIMIT ?""", (min(200, max(1, limit)),))


@app.post("/api/chats")
def create_chat(data: ChatInput) -> dict[str, Any]:
    from .services.assistant import ensure_chat
    return ensure_chat(db, None, title=data.title or "新对话", model=data.model, item_id=data.item_id)


@app.get("/api/chats/{chat_id}")
def chat_detail(chat_id: str) -> dict[str, Any]:
    from .services.assistant import messages_for
    chat = db.one("SELECT * FROM chats WHERE id=?", (chat_id,))
    if not chat:
        raise HTTPException(404, "对话不存在")
    return {"chat": chat, "messages": messages_for(db, chat_id)}


@app.patch("/api/chats/{chat_id}")
def update_chat(chat_id: str, data: ChatPatch) -> dict[str, Any]:
    chat = db.one("SELECT * FROM chats WHERE id=?", (chat_id,))
    if not chat:
        raise HTTPException(404, "对话不存在")
    db.execute("UPDATE chats SET title=?,model=?,pinned=?,updated_at=? WHERE id=?",
               (data.title if data.title is not None else chat["title"],
                data.model if data.model is not None else chat["model"],
                int(data.pinned) if data.pinned is not None else chat["pinned"],
                now_iso(), chat_id))
    return db.one("SELECT * FROM chats WHERE id=?", (chat_id,))


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str) -> dict[str, Any]:
    if not db.one("SELECT id FROM chats WHERE id=?", (chat_id,)):
        raise HTTPException(404, "对话不存在")
    db.execute("DELETE FROM chat_messages WHERE chat_id=?", (chat_id,))
    db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    return {"deleted": True, "id": chat_id}


CONTEXT_ERROR_MARKERS = ("context_length", "maximum context", "context length", "too many tokens",
                         "prompt is too long", "reduces the length", "reduce the length",
                         "exceeds the maximum", "input is too long", "上下文长度", "超出最大")


def _is_context_overflow(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in CONTEXT_ERROR_MARKERS)


def _chat_failure(db: Database, chat_id: str, exc: Exception, info: dict[str, Any]) -> dict[str, Any]:
    from .services.assistant import append_message, messages_for
    text = redact_secrets(str(exc))
    hint = ("（已自动压缩上下文并重试一次仍然超长。可以点「新对话」重开，"
            "或换一个窗口更大的模型。）" if _is_context_overflow(exc) else "")
    append_message(db, chat_id, "assistant", f"（调用失败：{text}）{hint}", {"error": True})
    return {"ok": False, "error": text, "chat_id": chat_id, "context": info,
            "messages": messages_for(db, chat_id)}


@app.post("/api/chats/{chat_id}/messages")
async def send_chat_message(chat_id: str, data: ChatSendInput) -> dict[str, Any]:
    from .services.assistant import (append_message, auto_title, build_messages, ensure_chat,
                                     fit_messages, messages_for)
    from .services.llm_gateway import complete, resolve
    from .services.websearch import gather, render_block
    question = data.content.strip()
    if not question:
        raise HTTPException(400, "消息为空")
    chat = ensure_chat(db, chat_id, item_id=data.item_id)
    item_id = data.item_id or chat.get("item_id")
    try:
        model_name = resolve(data.account).get("model") or ""
    except Exception:  # noqa: BLE001 - 账户还没配置时也允许先保存这条消息
        model_name = ""
    settings = get_settings()["llm"]
    window_override = int(settings.get("chat_context_tokens") or 0)

    # 联网检索在前：模型拿到的材料里就已经带上外部证据，不需要它自己会调工具。
    web: dict[str, Any] = {"results": [], "queries": [], "errors": [], "elapsed_ms": 0}
    if data.web_search:
        # 追问往往省略主语（"它到底用了哪种偶联化学"），把精读出的名词一起带进查询，
        # 否则检索词只剩几个泛词，搜回来的是同名不同事的内容。
        item_row = (db.one("SELECT title,analysis_json FROM items WHERE id=?", (item_id,))
                    if item_id else None)
        item_terms: list[str] = []
        try:
            for term in (json_loads((item_row or {}).get("analysis_json"), {}) or {}).get("terms") or []:
                if isinstance(term, dict) and term.get("term"):
                    item_terms.append(str(term["term"]))
        except Exception:  # noqa: BLE001 - 精读结果损坏不该拦住检索
            item_terms = []
        try:
            web = await gather(question, item_title=(item_row or {}).get("title") or "",
                               item_terms=item_terms)
        except Exception as exc:  # noqa: BLE001 - 检索失败不该拦住提问
            web["errors"] = [f"检索失败：{type(exc).__name__}: {str(exc)[:160]}"]
    web_block = render_block(web) if (web.get("results") or web.get("queries")) else ""
    messages, info = build_messages(
        db, chat["id"], question, item_id=item_id, selection=data.selection,
        web_block=web_block, model=model_name, configured_window=window_override)

    append_message(db, chat["id"], "user", question,
                   {"selection": data.selection[:2000], "item_id": data.item_id,
                    "web_search": bool(data.web_search),
                    "sources": [{k: h.get(k) for k in ("n", "title", "url", "source")}
                                for h in web.get("results") or []]})
    if chat["title"] in {"", "新对话"}:
        db.execute("UPDATE chats SET title=?,model=? WHERE id=?",
                   (auto_title(question), data.account or chat["model"], chat["id"]))
    try:
        result = await complete(messages, data.account, temperature=data.temperature, task="chat")
    except Exception as exc:
        # 上下文超长是唯一值得自动重来的错误：把预算砍半、正文压到最小再试一次。
        if _is_context_overflow(exc):
            messages, info = build_messages(
                db, chat["id"], question, item_id=item_id, selection=data.selection,
                web_block=web_block, model=model_name, configured_window=window_override,
                budget_scale=0.5)
            try:
                result = await complete(messages, data.account,
                                        temperature=data.temperature, task="chat")
                info["retried"] = True
            except Exception as exc2:
                return _chat_failure(db, chat["id"], exc2, info)
        else:
            return _chat_failure(db, chat["id"], exc, info)
    usage = result.get("usage") or {}
    # Paid models report the real deduction in `credit`; free ones only carry a
    # reference price in `original_credit`.
    points = usage.get("credit") or usage.get("original_credit")
    append_message(db, chat["id"], "assistant", result["content"],
                   {"model": result.get("model"), "label": result.get("label"),
                    "points": points, "elapsed_ms": result.get("elapsed_ms"),
                    "context": info, "sources": [
                        {k: h.get(k) for k in ("n", "title", "url", "source")}
                        for h in web.get("results") or []]})
    return {"ok": True, "chat_id": chat["id"], "reply": result["content"],
            "points": points, "model": result.get("label") or result.get("model"),
            "elapsed_ms": result.get("elapsed_ms"),
            "context": info, "search": {"queries": web.get("queries") or [],
                                        "errors": web.get("errors") or [],
                                        "elapsed_ms": web.get("elapsed_ms") or 0,
                                        "count": len(web.get("results") or [])},
            "messages": messages_for(db, chat["id"])}


@app.get("/api/chats/{chat_id}/export")
def export_chat(chat_id: str) -> Response:
    from .services.assistant import export_markdown, messages_for
    chat = db.one("SELECT * FROM chats WHERE id=?", (chat_id,))
    if not chat:
        raise HTTPException(404, "对话不存在")
    text = export_markdown(chat, messages_for(db, chat_id, 500))
    filename = f"chat-{chat['id'][:8]}.md"
    return Response(content=text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/notifications")
def notifications(limit: int = 12, include_all: bool = False) -> dict[str, Any]:
    """默认只给「要处理的 + 最近发出去的」，不把整部历史都铺出来。

    汇报记录一周能攒十几批，全量渲染会让页面越来越重。MERGED 的那批内容已经并进
    后面某一批（详情接口本来也拒绝渲染），留着只增加噪音，因此一律不列。
    反过来，没发出去的（OPEN/CLOSED/FAILED）无论多久都得露头，否则出问题没人看得见。
    """
    limit = 200 if include_all else max(1, min(200, limit))
    rows = db.query("""SELECT b.*,COUNT(n.item_id) item_count FROM notification_batches b
        LEFT JOIN notification_items n ON n.batch_id=b.id
        WHERE b.state IN ('OPEN','CLOSED','FAILED')
           OR (b.state='SENT' AND b.id IN (SELECT id FROM notification_batches
                                           WHERE state='SENT' ORDER BY created_at DESC LIMIT ?))
        GROUP BY b.id ORDER BY b.created_at DESC""", (limit,))
    total = int((db.one("SELECT COUNT(*) n FROM notification_batches WHERE state<>'MERGED'") or {"n": 0})["n"])
    return {"items": rows, "total": total, "shown": len(rows), "limit": limit,
            "include_all": include_all}


@app.get("/api/notifications/{batch_id}")
def notification_detail(batch_id: str) -> dict[str, Any]:
    from .services.digest import render_digest
    batch = db.one("SELECT state,last_error FROM notification_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "批次不存在")
    if batch["state"] == "MERGED":
        return {"text": "这一批已经并入最新汇报，避免同一批内容重复发送。请在列表里查看最新的「等待发送」批次。",
                "items": [], "channel": get_settings()["notifications"]["channel"]}
    text, items, channel = render_digest(db, batch_id, get_settings())
    return {"text": text, "items": items, "channel": channel}


@app.post("/api/notifications/flush")
def flush_notifications() -> dict[str, Any]:
    from .services.notify import merge_closed_batches
    closed = flush_open_batches(db, force_all=True)
    # Closed-but-unsent batches waiting on a window are folded into one message too;
    # otherwise "立即合并" left yesterday's digests parked in the queue.
    keep = merge_closed_batches(db)
    queued = 0
    if keep:
        queued = int(db.enqueue_job("deliver_batch", {"version": 1, "batch_id": keep},
                                    dedupe_key=f"deliver:{keep}:manual", priority=10, max_attempts=8))
    return {"batches": closed, "merged_into": keep, "queued": bool(queued)}


@app.post("/api/notifications/{batch_id}/retry")
def retry_notification(batch_id: str) -> dict[str, Any]:
    batch = db.one("SELECT id,state,priority FROM notification_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "批次不存在")
    if batch["state"] == "MERGED":
        # The items live in the surviving batch; retrying this row would send nothing.
        return {"queued": False, "reason": "该批次已并入最新汇报，无需单独重试"}
    db.enqueue_job("deliver_batch", {"version": 1, "batch_id": batch_id},
                   dedupe_key=f"deliver:{batch_id}:manual:{uuid.uuid4()}", priority=10, max_attempts=8)
    return {"queued": True}


@app.post("/api/notifications/test")
async def test_notification() -> dict[str, Any]:
    # 没配 webhook、webhook 写错、企业微信拒收，都是用户能自己处理的，
    # 直接把原因说清楚；500 只会让人对着 "Internal Server Error" 发呆。
    try:
        await send_test_notification()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@app.get("/api/events")
def events(limit: int = 100) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(300, max(1, limit)),))


@app.get("/api/jobs/summary")
def jobs_summary(limit: int = 150) -> dict[str, Any]:
    limit = min(300, max(20, limit))
    rows = db.query("""SELECT * FROM jobs ORDER BY
        CASE state WHEN 'LEASED' THEN 0 WHEN 'READY' THEN 1 WHEN 'RETRY_WAIT' THEN 2
                   WHEN 'DEAD' THEN 3 ELSE 4 END, updated_at DESC LIMIT ?""", (limit,))
    item_ids = []
    source_ids = []
    for job in rows:
        payload = json_loads(job.pop("payload_json", ""), {}) or {}
        job["payload"] = payload
        if payload.get("item_id"):
            item_ids.append(payload["item_id"])
        if payload.get("source_id"):
            source_ids.append(payload["source_id"])
    item_map, source_map = {}, {}
    if item_ids:
        for start in range(0, len(set(item_ids)), 300):
            chunk = list(set(item_ids))[start:start + 300]
            marks = ",".join("?" for _ in chunk)
            for item in db.query(f"SELECT id,title,status,url FROM items WHERE id IN ({marks})", chunk):
                item_map[item["id"]] = item
    if source_ids:
        marks = ",".join("?" for _ in set(source_ids))
        for source in db.query(f"SELECT id,name,type FROM sources WHERE id IN ({marks})", list(set(source_ids))):
            source_map[source["id"]] = source
    for job in rows:
        job["retryable"] = job_error_is_retryable(job.get("last_error") or "")
        payload = job["payload"]
        if payload.get("item_id") and payload["item_id"] in item_map:
            job["target_title"] = item_map[payload["item_id"]].get("title") or "未命名条目"
            job["target_url"] = item_map[payload["item_id"]].get("url")
            job["target_type"] = "内容"
        elif payload.get("source_id") and payload["source_id"] in source_map:
            job["target_title"] = source_map[payload["source_id"]].get("name") or "采集来源"
            job["target_type"] = "来源"
        elif payload.get("batch_id"):
            job["target_title"] = "汇报批次 " + str(payload["batch_id"])[:8]
            job["target_type"] = "通知"
        else:
            job["target_title"] = "系统任务"
            job["target_type"] = "系统"
    counts = {row["state"]: row["c"] for row in db.query(
        "SELECT state,COUNT(*) c FROM jobs GROUP BY state")}
    by_kind = db.query("""SELECT kind,state,COUNT(*) c FROM jobs
        WHERE state IN ('READY','LEASED','RETRY_WAIT','DEAD') GROUP BY kind,state ORDER BY c DESC""")
    recent_done = db.query("SELECT COUNT(*) c FROM jobs WHERE state='SUCCEEDED' AND updated_at>=?",
                           ((utcnow() - timedelta(hours=24)).isoformat(timespec="milliseconds"),))[0]["c"]
    heartbeat = db.one("SELECT value FROM runtime WHERE key='worker'")
    # 失败列表经常是"同一件事重复了几百次"。先按错误首段归并，用户才知道
    # 该修的是一个环境问题，还是几百篇不同的文章各自坏了。
    failed_groups = db.query("""SELECT substr(last_error,1,90) AS error,COUNT(*) AS count
        FROM jobs WHERE state='DEAD' AND last_error IS NOT NULL AND last_error<>''
        GROUP BY substr(last_error,1,90) ORDER BY count DESC LIMIT 6""")
    return {
        "counts": {
            "ready": counts.get("READY", 0), "running": counts.get("LEASED", 0),
            "retry": counts.get("RETRY_WAIT", 0), "failed": counts.get("DEAD", 0),
            "done_24h": recent_done, "total": sum(counts.values()),
        },
        "active": counts.get("READY", 0) + counts.get("LEASED", 0) + counts.get("RETRY_WAIT", 0),
        "by_kind": by_kind,
        "failed_groups": failed_groups,
        "jobs": rows,
        "worker_heartbeat": heartbeat["value"] if heartbeat else None,
    }


def _job_log_excerpt(job_id: str, limit: int = 24000) -> str:
    log_path = LOG_DIR / "worker.log"
    if not log_path.is_file():
        return "worker.log 不存在"
    text = log_path.read_text(encoding="utf-8", errors="replace")[-min(limit, 5_000_000):]
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines) if job_id in line]
    if matches:
        start = max(0, matches[0] - 20)
        end = min(len(lines), matches[-1] + 100)
        excerpt = "\n".join(lines[start:end])
    else:
        errors = [index for index, line in enumerate(lines) if "ERROR" in line or "Traceback" in line]
        excerpt = "\n".join(lines[max(0, errors[-1] - 50):]) if errors else "\n".join(lines[-120:])
    return redact_secrets(excerpt[-16000:])


def _repair_hint(message: str) -> tuple[str, str]:
    """按错误指纹给一条「先做什么」的提示。

    自动改代码不安全，但「这一条到底该等、该重试、还是该改代码」可以自动判。
    用户看到 800 条失败时，最需要的不是逐条日志，而是"这其实是同一件事"。
    """
    text = str(message or "").lower()
    patterns = (
        ("微信未登录 / 版本不适配",
         ("未能识别当前微信账号", "请先打开并登录微信", "请保持微信已登录"),
         "先打开微信确认已登录；仍然失败说明适配器不认识这个版本，需要更新读取器，不是重试能解决的。"),
        ("3.x 读取器被 4.x 拒绝",
         ("不会复用 3.x 密钥",),
         "这台机器是 4.x，强制走 3.x 会永远读不到。检查 local_channel()，不要设 RADAR_WECHAT_CHANNEL=3.x。"),
        ("标题或链接不完整",
         ("只有裸链接", "没有提取到有效正文", "空壳页面", "没有正文"),
         "先看链接是否带 chksm（带才给正文）；标题为空的多半是同批第二篇，启用账号下应放行，归档后会以正文标题覆盖。"),
        ("网关临时风控",
         ("502", "503", "504", "waf", "临时风控", "限流"),
         "临时错误，等几分钟自动重试即可；连续出现再查代理和网络。"),
        ("额度用尽",
         ("达到今日 llm 调用上限", "额度", "insufficient_balance"),
         "额度顺延到次日，不是文章坏了；需要当天跑完就在设置里提高上限。"),
        ("密钥或权限",
         ("401", "403", "invalid api key", "authentication"),
         "检查设置里的 API Key / 数据源账号；这类重试不会好。"),
    )
    for label, tokens, advice in patterns:
        if any(token in text for token in tokens):
            return label, advice
    return "", ""


@app.get("/api/jobs/{job_id}/diagnostics")
def job_diagnostics(job_id: str) -> dict[str, Any]:
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    payload = json_loads(job.get("payload_json"), {}) or {}
    target = payload.get("item_id") or payload.get("source_id") or payload.get("batch_id") or ""
    item = db.one("SELECT title,status,url,error FROM items WHERE id=?", (target,)) if payload.get("item_id") else None
    source = db.one("SELECT name,type,last_error FROM sources WHERE id=?", (target,)) if payload.get("source_id") else None
    log_excerpt = _job_log_excerpt(job_id)
    retryable = job_error_is_retryable(job.get("last_error") or "")
    hint_label, hint_advice = _repair_hint(job.get("last_error") or "")
    hint_block = (f"## 已知模式\n- 类型：{hint_label}\n- 先做：{hint_advice}\n\n" if hint_label else "")
    target_lines = []
    if item:
        target_lines = [f"- 文章：{item['title']}", f"- 原链接：{item['url']}", f"- 当前状态：{item['status']}",
                        f"- 条目错误：{item.get('error') or '无'}"]
    elif source:
        target_lines = [f"- 来源：{source['name']}（{source['type']}）", f"- 来源错误：{source.get('last_error') or '无'}"]
    markdown = f"""# Hermes 科研雷达任务失败诊断

请在当前 WorkBuddy 对话中根据下面信息定位根因并修复代码。不要只建议“重试”；需要指出具体文件、字段或异常路径，并给出最小补丁。修复后说明如何验证。

## 任务
- 任务 ID：{job['id']}
- 类型：{job['kind']}
- 状态：{job['state']}
- 尝试次数：{job['attempts']}/{job['max_attempts']}
- 错误：{job.get('last_error') or '无'}
- 是否像临时错误：{'是' if retryable else '否'}
- 参数：{stable_json(payload)}

{chr(10).join(target_lines)}

{hint_block}## 环境
- 项目：E:\\HermesResearchRadar
- Python：{sys.version.split()[0]}
- 系统：{platform.platform()}
- 数据库：{DATA_DIR / 'radar.db'}
- 日志：{LOG_DIR / 'worker.log'}

## 错误日志与堆栈
```text
{log_excerpt}
```

## 修复要求
1. 先解释根因，区分代码缺陷、外部接口/验证码、配置和临时网络问题。
2. 如果确认是代码问题，直接修改项目文件并运行相关测试。
3. 如果无法自动确认，列出需要我补充的日志或操作，不要盲目重试消耗付费额度。
"""
    return {"job_id": job_id, "retryable": retryable, "markdown": markdown,
            "hint": hint_label, "hint_advice": hint_advice,
            "log_excerpt": log_excerpt, "payload": payload}


@app.get("/api/jobs")
def jobs(state: str = "") -> list[dict[str, Any]]:
    if state:
        return db.query("SELECT * FROM jobs WHERE state=? ORDER BY updated_at DESC LIMIT 200", (state,))
    return db.query("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 200")


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict[str, Any]:
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    db.execute("UPDATE jobs SET state='READY',run_at=?,attempts=0,last_error=NULL,owner=NULL,lease_until=NULL WHERE id=?",
               (now_iso(), job_id))
    return {"queued": True}


class JobBulkInput(BaseModel):
    action: str = "retry"
    ids: list[str] = []
    all_failed: bool = False


@app.post("/api/jobs/bulk")
def jobs_bulk(data: JobBulkInput) -> dict[str, Any]:
    """失败任务的批量处置：重试或删除记录。

    失败列表以前只能一条一条点，几百条旧账根本清不动。这里按 id 批量来；
    ``all_failed`` 是「清空全部失败」用的入口，只动 DEAD，绝不碰正在跑的任务。
    删除只删任务记录——资料库里的条目、正文和图片都不受影响。
    """
    action = (data.action or "").strip().lower()
    if action not in {"retry", "delete"}:
        raise HTTPException(400, "action 只能是 retry 或 delete")
    ids = [str(value).strip() for value in data.ids if str(value).strip()]
    if data.all_failed:
        ids = [row["id"] for row in db.query("SELECT id FROM jobs WHERE state='DEAD'")]
    if not ids:
        raise HTTPException(400, "没有选中失败任务")
    ids = ids[:2000]
    marks = ",".join("?" for _ in ids)
    if action == "retry":
        updated = db.execute(f"""UPDATE jobs SET state='READY',run_at=?,attempts=0,last_error=NULL,
            owner=NULL,lease_until=NULL,updated_at=? WHERE state='DEAD' AND id IN ({marks})""",
                             [now_iso(), now_iso(), *ids]) or 0
        db.add_event("jobs_retried", f"批量重试 {updated} 个失败任务")
        return {"action": action, "updated": updated}
    updated = db.execute(f"""DELETE FROM jobs WHERE state IN ('DEAD','CANCELLED','RETRY_WAIT')
        AND id IN ({marks})""", ids) or 0
    db.add_event("jobs_deleted", f"批量删除 {updated} 条失败任务记录（不影响资料库内容）")
    return {"action": action, "updated": updated}


@app.get("/api/llm/workbuddy/models")
async def workbuddy_models() -> dict[str, Any]:
    from .services.workbuddy_llm import list_workbuddy_models
    return {"models": await list_workbuddy_models()}


class LlmTestInput(BaseModel):
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""


@app.get("/api/setup/status")
def setup_status(refresh: bool = False) -> dict[str, Any]:
    """配置向导的整体状态。默认复用 90 秒内的快照，``?refresh=1`` 强制重新体检。"""
    from .services import setup_status as status
    return status.snapshot(force=refresh)


@app.post("/api/setup/test/llm")
async def setup_test_llm(data: LlmTestInput) -> dict[str, Any]:
    from .services.setup_test import test_llm
    settings = get_settings()["llm"]
    provider = data.provider or settings.get("provider", "openai_compatible")
    base_url = data.base_url or (settings.get("base_url") if provider == "openai_compatible" else "")
    model = data.model or settings.get("model", "")
    key = data.api_key or get_secret("llm_api_key")
    return await test_llm(provider, base_url, model, key)


@app.post("/api/setup/test/wechat")
def setup_test_wechat() -> dict[str, Any]:
    from .services.setup_test import test_wechat
    return test_wechat()


@app.post("/api/setup/wechat4/probe")
def setup_wechat4_probe() -> dict[str, Any]:
    """微信 4.x 的只读解锁验证：扫内存取密钥、逐个库试开，不读正文。"""
    from .services import wechat4
    return wechat4.probe()


@app.post("/api/setup/wechat/enable")
def setup_enable_wechat() -> dict[str, Any]:
    """把「只读本机公众号」这个来源建好并打开，向导里一键完成。"""
    from .utils import now_iso, stable_json
    existing = (db.query("SELECT * FROM sources WHERE type='wechat_public_local' ORDER BY created_at") or [None])[0]
    if existing:
        db.execute("UPDATE sources SET enabled=1,next_poll_at=COALESCE(next_poll_at,?),updated_at=? WHERE id=?",
                   (now_iso(), now_iso(), existing["id"]))
        db.add_event("source_enabled", "配置向导：重新启用本机公众号读取", source_id=existing["id"])
        return {"created": False, **_source_view(db.one("SELECT * FROM sources WHERE id=?", (existing["id"],)))}
    source_id = str(uuid.uuid4())
    timestamp = now_iso()
    db.execute("""INSERT INTO sources(id,type,name,config_json,enabled,poll_seconds,next_poll_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
               (source_id, "wechat_public_local", "本机微信公众号", stable_json({"use_catalog": True, "start_at_now": True}),
                1, 300, timestamp, timestamp, timestamp))
    db.add_event("source_created", "配置向导：创建本机公众号读取", source_id=source_id)
    return {"created": True, **_source_view(db.one("SELECT * FROM sources WHERE id=?", (source_id,)))}


@app.get("/api/setup/autostart")
def setup_autostart() -> dict[str, Any]:
    from .services import autostart
    return autostart.status()


@app.post("/api/setup/autostart")
def setup_autostart_toggle(data: dict[str, Any]) -> dict[str, Any]:
    from .services import autostart
    wanted = bool(data.get("enabled"))
    try:
        return autostart.enable() if wanted else autostart.disable()
    except OSError as exc:
        raise HTTPException(500, f"改开机自启失败：{exc}") from exc


@app.get("/api/settings")
def settings_view() -> dict[str, Any]:
    return {"settings": get_settings(), "secrets": secret_status(), "restart_required_for": ["host", "port", "worker_concurrency"]}


@app.put("/api/settings")
def settings_update(patch: dict[str, Any]) -> dict[str, Any]:
    patch.pop("schema_version", None)
    return {"settings": save_settings(patch), "secrets": secret_status()}


@app.post("/api/settings/secrets")
def secret_update(data: SecretInput) -> dict[str, Any]:
    try:
        set_secret(data.name, data.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Saving a key must never silently re-enable a paid source; enabling stays explicit
    # in 采集来源, so the toggle there is the single source of truth.
    return {"secrets": secret_status()}


# --------------------------------------------------------------------------- #
# 自动更新
# --------------------------------------------------------------------------- #
@app.get("/api/update")
def update_status() -> dict[str, Any]:
    return updater.status()


@app.post("/api/update/check")
def update_check(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """同步查一次。放在 def 而不是 async def，FastAPI 会丢进线程池，不挡事件循环。"""
    timeout = float((data or {}).get("timeout") or 6)
    return updater.check(timeout=max(3.0, min(timeout, 20.0)))


@app.post("/api/update/download")
def update_download(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """后台下载，立刻返回。进度靠 GET /api/update 轮询。"""
    state = updater.status()
    version = (data or {}).get("version") or state.get("latest") or ""
    url = (data or {}).get("url") or state.get("url") or ""
    if not version or not url:
        raise HTTPException(400, "还没有可下载的版本，先检查更新。")
    running = updater.download_state()
    if running.get("state") == "downloading":
        return {"started": False, "reason": "已经在下了", "download": running}

    def worker() -> None:
        try:
            updater.download(url, version, state.get("sha256") or "")
        except Exception:  # noqa: BLE001 - 失败信息已经写进进度
            pass

    threading.Thread(target=worker, daemon=True, name="update-download").start()
    return {"started": True, "version": version}


@app.post("/api/update/apply")
def update_apply(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """静默覆盖安装。返回后几百毫秒内本进程就会退出（supervisor 收到停止信号）。"""
    version = (data or {}).get("version") or updater.status().get("latest") or ""
    try:
        return updater.apply(version, restart=bool((data or {}).get("restart", True)))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"启动更新失败：{exc}") from exc


def main() -> None:
    import uvicorn
    settings = get_settings()
    uvicorn.run(app, host=settings["host"], port=int(settings["port"]), log_config=None, access_log=False)


if __name__ == "__main__":
    main()
