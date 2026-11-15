import time
from typing import Any
from urllib.parse import urlencode

from ..net import get_json, post_json
from ..settings import get_secret
from ..utils import canonical_url, epoch_iso, extract_urls, json_loads

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


async def _access_token(corp_id: str) -> str:
    secret = get_secret("wecom_kf_secret")
    if not secret:
        raise RuntimeError("未配置微信客服 Secret")
    cache_key = corp_id + ":" + secret[-8:]
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > time.time() + 120:
        return cached[0]
    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken?" + urlencode({
        "corpid": corp_id, "corpsecret": secret,
    })
    payload, _ = await get_json(url)
    if payload.get("errcode") != 0 or not payload.get("access_token"):
        raise RuntimeError(f"企业微信获取 token 失败：{payload.get('errcode')} {payload.get('errmsg')}")
    token = payload["access_token"]
    _TOKEN_CACHE[cache_key] = (token, time.time() + int(payload.get("expires_in", 7200)))
    return token


async def poll_wecom_kf(source: dict[str, Any], scope: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = json_loads(source.get("config_json"), {}) or {}
    corp_id = str(config.get("corp_id") or "").strip()
    open_kf_id = str(config.get("open_kf_id") or "").strip()
    if not corp_id or not open_kf_id:
        raise ValueError("微信客服来源需要 corp_id 和 open_kf_id")
    token = await _access_token(corp_id)
    cursor_state = json_loads(source.get("cursor_json"), {}) or {}
    sync_cursor = cursor_state.get("sync_cursor", "")
    prime_complete = bool(cursor_state.get("prime_complete", not config.get("start_at_now", True)))
    candidates: list[dict[str, Any]] = []
    for _ in range(10):
        payload = {"limit": 1000, "open_kfid": open_kf_id, "voice_format": 0}
        if sync_cursor:
            payload["cursor"] = sync_cursor
        endpoint = "https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?" + urlencode({"access_token": token})
        response, _ = await post_json(endpoint, payload, timeout=40)
        if response.get("errcode") != 0:
            code = response.get("errcode")
            if sync_cursor and code in {40058, 40003, 41001, 42001}:
                # Expired cursor: fetch everything still available in the 3-day window.
                sync_cursor = ""
                prime_complete = True
                continue
            raise RuntimeError(f"微信客服读取消息失败：{code} {response.get('errmsg')}")
        for message in response.get("msg_list") or []:
            if message.get("origin") != 3 or not prime_complete:
                continue
            msgtype = message.get("msgtype")
            url = ""
            title = "微信分享"
            description = ""
            if msgtype == "link":
                link = message.get("link") or {}
                url = str(link.get("url") or "").strip()
                title = str(link.get("title") or title).strip()
                description = str(link.get("desc") or "")
            elif msgtype == "text":
                text = str((message.get("text") or {}).get("content") or "")
                urls = extract_urls(text)
                url = urls[0] if urls else ""
                title = text[:120] or title
                description = text
            if not url:
                continue
            candidates.append({
                "source_id": source["id"], "kind": "video" if "bilibili.com" in url or "b23.tv/" in url else "article",
                "url": url, "canonical_url": canonical_url(url), "external_key": str(message.get("msgid") or canonical_url(url)),
                "title": title, "author": "微信客服转发", "published_at": epoch_iso(message.get("send_time")),
                "metadata": {"share_channel": "wecom_customer_service", "wecom_msgid": message.get("msgid"),
                             "wecom_msgtype": msgtype, "description": description},
            })
        next_cursor = response.get("next_cursor") or sync_cursor
        has_more = bool(response.get("has_more"))
        sync_cursor = next_cursor
        if not has_more:
            prime_complete = True
            break
    return candidates, {"sync_cursor": sync_cursor, "prime_complete": prime_complete}
