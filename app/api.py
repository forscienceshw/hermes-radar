import csv
import io
import json
import os
import platform
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .collectors import COLLECTORS
from .db import Database, row_with_json
from .logging_config import configure_logging
from .paths import BLOB_DIR, DATA_DIR, LOG_DIR, WEB_DIR
from .services.notify import flush_open_batches, send_test_notification
from .services.relevance import assess_candidate
from .settings import get_secret, get_settings, save_settings, secret_status, set_secret
from .utils import (extract_bvid, extract_urls, job_error_is_retryable, json_loads, local_date, now_iso,
                    parse_time, redact_secrets, stable_json, utcnow)

configure_logging("web")
db = Database()
app = FastAPI(title="Hermes 科研雷达", version=__version__)
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
    enabled: bool


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
    return {
        "total": counts.get("total") or 0, "discovered": counts.get("discovered") or 0,
        "index_only": counts.get("index_only") or 0, "archived": counts.get("archived") or 0,
        "ready": counts.get("ready") or 0, "filtered": counts.get("filtered") or 0, "errors": counts.get("errors") or 0,
        "archive_queued": archive_queued, "archive_running": int(queued.get("archive_running") or 0),
        "analyze_queued": int(queued.get("analyze_queued") or 0), "analyze_running": int(queued.get("analyze_running") or 0),
        "archives_last_10min": int(recent.get("archives") or 0), "analyses_last_10min": int(recent.get("analyses") or 0),
        "archives_per_minute": archives_per_min, "eta_seconds": eta, "running": running,
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
    remaining = db.one("""SELECT COUNT(*) c FROM items WHERE status='discovered' AND excluded_at IS NULL""")["c"]
    if not remaining:
        return {"queued": False, "remaining": 0}
    running = db.one("""SELECT id FROM jobs WHERE kind='archive_history_batch' AND state IN ('READY','RETRY_WAIT','LEASED')""")
    if running:
        return {"queued": True, "already_running": True, "remaining": remaining, "llm_calls": 0}
    db.enqueue_job("archive_history_batch", {"version": 1, "batch_size": 8},
                   dedupe_key=f"history-archive-start:all:{uuid.uuid4()}", priority=200, max_attempts=3)
    return {"queued": True, "remaining": remaining, "llm_calls": 0}


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


@app.patch("/api/wechat/accounts/{account_pk}")
def update_wechat_account(account_pk: int, data: WeChatAccountPatch) -> dict[str, Any]:
    if not db.one("SELECT id FROM wechat_accounts WHERE id=?", (account_pk,)):
        raise HTTPException(404, "公众号不存在")
    db.execute("UPDATE wechat_accounts SET enabled=?,updated_at=? WHERE id=?",
               (int(data.enabled), now_iso(), account_pk))
    return {"updated": 1}


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
    for job in db.query("""SELECT kind,state,payload_json FROM jobs
        WHERE state IN ('LEASED','READY','RETRY_WAIT') AND kind IN ('archive_item','papers_item','analyze_item')
        ORDER BY (state='LEASED') DESC, created_at"""):
        payload = json_loads(job["payload_json"], {}) or {}
        item_id = payload.get("item_id")
        if item_id and item_id not in busy:
            busy[item_id] = "analyze" if job["kind"] == "analyze_item" else "archive"
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
    item["active_job"] = ("analyze" if job["kind"] == "analyze_item" else "archive") if active else None
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
        db.set_items_excluded([item_id], "手动恢复收录", excluded=False)
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
def papers(status: str = "") -> list[dict[str, Any]]:
    if status == "needed":
        condition = "WHERE p.status IN ('closed','failed','not_found')"
        params: tuple = ()
    elif status:
        condition = "WHERE p.status=?"
        params = (status,)
    else:
        condition, params = "", ()
    return db.query(f"""SELECT p.*,(SELECT i.title FROM items i JOIN item_papers ip ON i.id=ip.item_id
        WHERE ip.paper_id=p.id LIMIT 1) source_title,(SELECT i.url FROM items i JOIN item_papers ip ON i.id=ip.item_id
        WHERE ip.paper_id=p.id LIMIT 1) source_url FROM papers p {condition} ORDER BY p.created_at DESC""", params)


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
def notifications() -> list[dict[str, Any]]:
    return db.query("""SELECT b.*,COUNT(n.item_id) item_count FROM notification_batches b
        LEFT JOIN notification_items n ON n.batch_id=b.id GROUP BY b.id ORDER BY b.created_at DESC LIMIT 100""")


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
    return {
        "counts": {
            "ready": counts.get("READY", 0), "running": counts.get("LEASED", 0),
            "retry": counts.get("RETRY_WAIT", 0), "failed": counts.get("DEAD", 0),
            "done_24h": recent_done, "total": sum(counts.values()),
        },
        "active": counts.get("READY", 0) + counts.get("LEASED", 0) + counts.get("RETRY_WAIT", 0),
        "by_kind": by_kind,
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

## 环境
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
def setup_status() -> dict[str, Any]:
    from .services import setup_status as status
    return status.snapshot()


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


def main() -> None:
    import uvicorn
    settings = get_settings()
    uvicorn.run(app, host=settings["host"], port=int(settings["port"]), log_config=None, access_log=False)


if __name__ == "__main__":
    main()
