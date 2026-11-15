import calendar
import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import feedparser

from ..net import get_json, request
from ..settings import get_secret
from .wecom_kf import poll_wecom_kf
from ..services.wechat_self_archive import poll_wechat_public_local, poll_wechat_self
from ..utils import canonical_url, extract_bvid, extract_urls, json_loads, now_iso, parse_time, utcnow

CAPABILITIES = {
    "rss": "持续监测 RSS/Atom；也可接 RSSHub 等提供的公众号或 UP 主订阅源",
    "wechat_datax": "通过可选第三方 DataXAPI 监测明确配置的公众号；不能读取微信个人关注列表",
    "jsonl": "实时尾随分享桥 JSONL，自动提取其中的网页、公众号和B站链接",
    "bilibili_datax": "通过可选第三方 DataXAPI 按科研关键词或 UP 主 mid 持续发现视频",
    "wecom_kf": "企业微信微信客服：接收你从微信转发到客服会话的文章链接，可回读最近3天消息",
    "wechat_local": "只读当前微信3.x指定聊天窗口（含WAL）；历史链接默认仅建索引，新转发自动处理；密钥由DPAPI保护",
    "wechat_public_local": "只读当前微信3.x PublicMsg；联动公众号库启用名单，按localId增量追新，无需第三方API",
}


def _config(source: dict[str, Any]) -> dict[str, Any]:
    return json_loads(source.get("config_json"), {}) or {}


def _candidate(url: str, source: dict[str, Any], **extra: Any) -> dict[str, Any]:
    kind = "video" if extract_bvid(url) or "b23.tv/" in url else extra.pop("kind", "article")
    return {
        "source_id": source["id"], "kind": kind, "url": url,
        "canonical_url": canonical_url(url), "external_key": extra.pop("external_key", canonical_url(url)),
        **extra,
    }


async def poll_rss(source: dict[str, Any], scope: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = _config(source)
    response = await request(config["url"], max_bytes=20_000_000,
                             headers={"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"})
    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"RSS 解析失败：{feed.bozo_exception}")
    candidates = []
    for entry in feed.entries[:100]:
        url = entry.get("link")
        if not url:
            continue
        published = None
        if entry.get("published_parsed"):
            published = now_iso() if not entry.published_parsed else __import__("datetime").datetime.fromtimestamp(
                calendar.timegm(entry.published_parsed), __import__("datetime").timezone.utc).isoformat()
        candidates.append(_candidate(
            url, source, kind=config.get("kind", "article"), external_key=entry.get("id") or url,
            title=entry.get("title") or "", author=entry.get("author") or feed.feed.get("title") or "",
            published_at=published, metadata={"feed_title": feed.feed.get("title") or ""},
        ))
    return candidates, json_loads(source.get("cursor_json"), {}) or {}


def _datax_headers(scope: str, label: str, max_price: str) -> dict[str, str]:
    key = get_secret("datax_api_key")
    if not key:
        raise RuntimeError("未配置 DataXAPI Key；该来源保持停用状态，不会使用非官方爬虫替代")
    idem = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}:{label}"))
    return {"Authorization": f"Bearer {key}", "Idempotency-Key": idem, "X-Max-Price": max_price}


async def _resolve_account(account: dict[str, Any], source: dict[str, Any], scope: str,
                           cursor: dict[str, Any]) -> dict[str, Any]:
    if account.get("id"):
        return account
    cache = cursor.setdefault("account_ids", {})
    name = account["name"].strip()
    if name in cache:
        return {**account, "id": cache[name]}
    url = "https://api.dataxapi.com/v1/wechat/accounts/search?" + urlencode({"query": name})
    payload, _ = await get_json(url, headers=_datax_headers(scope, "search:" + name, "0.10"))
    items = (payload.get("data") or {}).get("items") or []
    exact = [item for item in items if (item.get("name") or "").strip().casefold() == name.casefold()
             or (item.get("username") or "").strip().casefold() == name.casefold()]
    if len(exact) != 1:
        choices = [{"id": x.get("id"), "name": x.get("name"), "username": x.get("username")} for x in items[:8]]
        raise ValueError("公众号不能唯一匹配，请在来源配置中填入准确 id；候选：" + json.dumps(choices, ensure_ascii=False))
    cache[name] = exact[0]["id"]
    return {**account, "id": exact[0]["id"], "name": exact[0].get("name") or name}


def _schedule_next_check(account: dict[str, Any], config: dict[str, Any], published_times: list[Any], now: Any) -> tuple[str, int]:
    from datetime import datetime, timezone
    import statistics
    fixed = account.get("interval_seconds") or config.get("account_interval_seconds")
    if fixed:
        seconds = max(30, int(fixed))
    elif not config.get("adaptive_polling", True):
        seconds = max(30, int(config.get("poll_seconds") or 300))
    else:
        intervals = []
        ordered = sorted(published_times, reverse=True)
        for newer, older in zip(ordered, ordered[1:]):
            delta = (newer - older).total_seconds()
            if delta > 0:
                intervals.append(delta)
        median = statistics.median(intervals) if intervals else 7 * 86400
        if median <= 3 * 3600:
            seconds = 300
        elif median <= 12 * 3600:
            seconds = 900
        elif median <= 86400:
            seconds = 1800
        elif median <= 3 * 86400:
            seconds = 7200
        elif median <= 7 * 86400:
            seconds = 21600
        else:
            seconds = 43200
    return (now + timedelta(seconds=seconds)).isoformat(timespec="milliseconds"), seconds


async def poll_wechat_datax(source: dict[str, Any], scope: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = _config(source)
    cursor = json_loads(source.get("cursor_json"), {}) or {}
    if config.get("use_catalog"):
        from ..db import Database
        accounts = Database().query("""SELECT name,account_id AS id FROM wechat_accounts
            WHERE enabled=1 ORDER BY category_code,name""")
        if not accounts:
            raise ValueError("公众号库中还没有启用任何公众号")
        max_accounts = int(config.get("max_accounts_per_run", 0) or 0)
        if max_accounts > 0 and len(accounts) > max_accounts:
            offset = int(cursor.get("catalog_offset", 0)) % len(accounts)
            accounts = [accounts[(offset + i) % len(accounts)] for i in range(max_accounts)]
            cursor["catalog_offset"] = (offset + max_accounts) % len(accounts)
    else:
        accounts = [a if isinstance(a, dict) else {"name": str(a)} for a in config.get("accounts", [])]
    if not accounts:
        raise ValueError("请至少配置一个公众号 name 和 id；系统无法自动读取微信关注列表")
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    states = cursor.setdefault("accounts", {})
    max_price = str(config.get("max_price_per_call", "0.05"))
    max_pages = max(1, min(20, int(config.get("max_pages_per_account_per_run", 3))))
    initial_days = max(1, min(365, int(config.get("initial_backfill_days", 30))))
    overlap_hours = max(0, min(168, int(config.get("overlap_hours", 24))))
    candidate_limit = max(0, int(config.get("max_candidates_per_account", 0) or 0))
    now = utcnow()
    successful_accounts = 0
    skipped_accounts = 0
    backfill_pending = False
    for account in accounts:
        account_label = str(account.get("name") or account.get("id") or "未知公众号")
        try:
            resolved = await _resolve_account(account, source, scope, cursor)
            account_id = str(resolved["id"])
            candidate_start = len(candidates)
            state = states.get(account_id, {})
            resume_cursor = state.get("next_cursor")
            next_check = parse_time(state.get("next_check_at"))
            if next_check and now < next_check and not resume_cursor:
                skipped_accounts += 1
                continue
            if resume_cursor and state.get("backfill_since"):
                since = parse_time(state["backfill_since"])
            else:
                last_success = parse_time(state.get("last_success_at")) or parse_time(source.get("last_success_at"))
                since = (last_success - timedelta(hours=overlap_hours)) if last_success else (now - timedelta(days=initial_days))
            next_cursor = resume_cursor
            reached_since = False
            has_more = True
            published_times = []
            for page_index in range(max_pages):
                params: dict[str, Any] = {"id": account_id}
                if next_cursor:
                    params["cursor"] = next_cursor
                payload, _ = await get_json(
                    "https://api.dataxapi.com/v1/wechat_mp/accounts/articles/list?" + urlencode(params),
                    headers=_datax_headers(scope, "articles:" + account_id + ":" + str(next_cursor or "first"), max_price),
                )
                items = (payload.get("data") or {}).get("items") or []
                for article in items:
                    url = article.get("url")
                    if not url:
                        continue
                    published = parse_time(article.get("published_at"))
                    if published:
                        published_times.append(published)
                    if since and published and published < since:
                        reached_since = True
                        break
                    candidates.append(_candidate(
                        url, source, external_key=str(article.get("id") or canonical_url(url)),
                        title=article.get("title") or "", author=resolved.get("name") or account.get("name") or "",
                        published_at=article.get("published_at"),
                        metadata={"wechat_account_id": account_id, "wechat_article_id": article.get("id"),
                                  "discovery_api": "DataXAPI", "backfill_since": since.isoformat() if since else None},
                    ))
                page = payload.get("page") or {}
                has_more = bool(page.get("has_more"))
                next_cursor = page.get("next_cursor")
                if reached_since or not has_more or not next_cursor:
                    break
            if candidate_limit > 0 and len(candidates) - candidate_start > candidate_limit:
                del candidates[candidate_start + candidate_limit:]
            if reached_since or not has_more or not next_cursor:
                state["next_cursor"] = None
                state.pop("backfill_since", None)
                state["last_success_at"] = now_iso()
                state["next_check_at"], state["check_interval_seconds"] = _schedule_next_check(
                    resolved, {**config, "poll_seconds": source.get("poll_seconds") or 300}, published_times, now)
            else:
                # Page budget exhausted while relevant history remains. Continue in the next immediate run.
                state["next_cursor"] = next_cursor
                state["backfill_since"] = since.isoformat() if since else None
                backfill_pending = True
            state["latest_checked_at"] = now_iso()
            states[account_id] = state
            successful_accounts += 1
        except Exception as exc:
            errors.append(f"{account_label}：{exc}")
    if not successful_accounts and not skipped_accounts:
        raise RuntimeError("；".join(errors)[:3500] or "没有公众号成功完成检查")
    cursor["backfill_pending"] = backfill_pending
    warnings = errors.copy()
    if backfill_pending:
        warnings.append("仍有历史分页未追完，将在 5 秒后自动继续")
    cursor["warnings"] = warnings
    return candidates, cursor


async def poll_jsonl(source: dict[str, Any], scope: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = _config(source)
    path = Path(config["path"]).expanduser().resolve()
    cursor = json_loads(source.get("cursor_json"), {}) or {}
    size = path.stat().st_size
    offset = int(cursor.get("offset", 0))
    if not cursor.get("initialized"):
        offset = size if config.get("start_at_end", False) else 0
    if offset > size:
        offset = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(10_000_001)
    if len(data) > 10_000_000:
        raise ValueError("JSONL 单次新增超过 10 MB，请检查分享桥是否写入异常")
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return [], {**cursor, "offset": offset, "initialized": True}
    complete = data[:last_newline + 1]
    new_offset = offset + len(complete)
    candidates = []
    for raw_line in complete.splitlines()[-1000:]:
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line.decode("utf-8-sig"))
            if not isinstance(record, dict):
                record = {"message": record}
            text = "\n".join(str(record.get(key, "")) for key in ("url", "text", "shared_text", "content", "message"))
            if not extract_urls(text):
                text = json.dumps(record, ensure_ascii=False)
        except (json.JSONDecodeError, UnicodeDecodeError):
            text = raw_line.decode("utf-8", errors="replace")
            record = {}
        for url in extract_urls(text):
            candidates.append(_candidate(
                url, source, title=str(record.get("title") or ""),
                published_at=record.get("timestamp") or record.get("created_at"),
                metadata={"share_record": record},
            ))
    return candidates, {**cursor, "offset": new_offset, "initialized": True}


async def poll_bilibili_datax(source: dict[str, Any], scope: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = _config(source)
    queries = [str(x).strip() for x in config.get("queries", []) if str(x).strip()]
    up_ids = [str(x).strip() for x in config.get("up_ids", []) if str(x).strip()]
    if not queries and not up_ids:
        raise ValueError("请至少配置一个 B站科研关键词 queries 或 UP 主 mid（up_ids）")
    max_price = str(config.get("max_price_per_call", "0.01"))
    requests = [("search", {"query": query, "sort": config.get("sort", "latest"), "view": "full"},
                 "https://api.dataxapi.com/v1/bilibili/videos/search") for query in queries]
    requests += [("up", {"id": mid, "view": "full"}, "https://api.dataxapi.com/v1/bilibili/users/videos/list") for mid in up_ids]
    candidates = []
    for label, params, endpoint in requests:
        payload, _ = await get_json(endpoint + "?" + urlencode(params),
                                    headers=_datax_headers(scope, label + ":" + json.dumps(params, sort_keys=True), max_price))
        for video in (payload.get("data") or {}).get("items") or []:
            url = video.get("url") or (f"https://www.bilibili.com/video/{video['id']}" if video.get("id") else "")
            if not url:
                continue
            metrics = video.get("metrics") or {}
            ext = video.get("ext") or {}
            searchable = " ".join([video.get("title") or "", video.get("text") or "", " ".join(video.get("hashtags") or [])]).lower()
            if int(metrics.get("views") or 0) < int(config.get("min_views", 0)):
                continue
            if float(ext.get("duration") or 0) < float(config.get("min_duration_seconds", 0)):
                continue
            if config.get("include_keywords") and not any(str(x).lower() in searchable for x in config["include_keywords"]):
                continue
            if any(str(x).lower() in searchable for x in config.get("exclude_keywords", [])):
                continue
            author = video.get("author") or {}
            candidates.append(_candidate(
                url, source, kind="video", external_key=str(video.get("id") or canonical_url(url)),
                title=video.get("title") or "", author=author.get("name") or author.get("id") or "",
                published_at=video.get("published_at"),
                metadata={"discovery_api": "DataXAPI", "discovery_query": params,
                          "metrics": metrics, "video_ext": ext, "description_preview": video.get("text") or ""},
            ))
    return candidates, json_loads(source.get("cursor_json"), {}) or {}


COLLECTORS = {
    "rss": poll_rss,
    "wechat_datax": poll_wechat_datax,
    "jsonl": poll_jsonl,
    "bilibili_datax": poll_bilibili_datax,
    "wecom_kf": poll_wecom_kf,
    "wechat_local": poll_wechat_self,
    "wechat_public_local": poll_wechat_public_local,
}


async def poll_source(source: dict[str, Any], scope: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    collector = COLLECTORS.get(source["type"])
    if not collector:
        raise ValueError(f"未知采集器：{source['type']}")
    return await collector(source, scope)
