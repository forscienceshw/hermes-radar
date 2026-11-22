"""公众号详情页的「先给东西看，再慢慢读」。

2026-11-22 用户报的问题：**首次启动后第一次点某个号，详情页像卡死**，关掉再点
就正常了。原因不是死锁——第一次要扫一遍 ``Weixin.exe`` 的内存（1.1 GB，冷启动
几秒到十几秒），顺手还要解密/查询微信库；那一次请求没回来之前，弹层里只有一行
「正在读取…」，看起来就是没反应。第二次能开，是因为第一次即使被关掉也在后台
把它算完了，缓存已经写好。

所以这里把一次点击拆成两段：

* ``peek()``   只读数据库里的缓存 + 两三个毫秒级的信号，立刻返回；缓存不新就
  顺手排一个后台线程去重读（同一个号同时只跑一个）。
* 网页每 1.2 秒再问一次 ``peek()``，后台读完自然就拿到新列表。

顺带把「到底为什么没有内容」说清楚：微信没运行、号没关注、真的很久没推、
读取失败——这四种在用户眼里都是「列表是空的」，但处理方式完全不同。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import wechat_panel

# 后台刷新最长算「还活着」的时间。超过就当线程卡死，允许再排一次。
STUCK_AFTER = 180.0
# 失败信息留给前端看多久。超过就不再提，免得用户以为现在还是坏的。
ERROR_TTL = 600.0

_INFLIGHT: dict[int, dict[str, Any]] = {}
_LAST: dict[int, dict[str, Any]] = {}
_LOCK = threading.Lock()

_STATUS_TTL = 30.0
_status: dict[str, Any] = {"at": 0.0, "value": {}}
_resolve: dict[int, dict[str, Any]] = {}
_RESOLVE_TTL = 300.0


def _now() -> float:
    return time.time()


def is_refreshing(account_pk: int) -> bool:
    with _LOCK:
        info = _INFLIGHT.get(int(account_pk))
        if not info:
            return False
        if _now() - float(info.get("at") or 0) > STUCK_AFTER:
            _INFLIGHT.pop(int(account_pk), None)
            return False
        return True


def last_error(account_pk: int) -> str:
    with _LOCK:
        info = _LAST.get(int(account_pk)) or {}
    if not info or info.get("ok"):
        return ""
    if _now() - float(info.get("at") or 0) > ERROR_TTL:
        return ""
    return str(info.get("error") or "")


def schedule(db: Any, account: dict[str, Any]) -> bool:
    """排一次后台重读。已经有一个在跑就返回 False，不叠线程。"""
    pk = int(account["id"])
    with _LOCK:
        info = _INFLIGHT.get(pk)
        if info and _now() - float(info.get("at") or 0) <= STUCK_AFTER:
            return False
        _INFLIGHT[pk] = {"at": _now()}

    snapshot = dict(account)

    def run() -> None:
        try:
            wechat_panel.refresh_cached(db, snapshot)
            with _LOCK:
                _LAST[pk] = {"at": _now(), "ok": True, "error": ""}
        except Exception as exc:                     # noqa: BLE001 - 失败要留给页面解释
            with _LOCK:
                _LAST[pk] = {"at": _now(), "ok": False, "error": str(exc)}
        finally:
            with _LOCK:
                _INFLIGHT.pop(pk, None)

    threading.Thread(target=run, daemon=True, name=f"wa-detail-{pk}").start()
    return True


def _gh_for(db: Any, account: dict[str, Any]) -> tuple[str, str]:
    """``resolve_gh`` 会读一次联系人表；同一个号 5 分钟内不重复算。"""
    pk = int(account["id"])
    cached = _resolve.get(pk)
    if cached and _now() - float(cached.get("at") or 0) < _RESOLVE_TTL:
        return str(cached.get("gh") or ""), str(cached.get("channel") or "")
    gh_id, channel = wechat_panel.resolve_gh(db, account)
    if gh_id:
        # 只缓存「对上了」的结果：这次没关注、名字对不上，不能锁死五分钟，
        # 用户可能刚在微信里关注完就回来点开。
        _resolve[pk] = {"at": _now(), "gh": gh_id, "channel": channel}
    return gh_id, channel


def forget(account_pk: int) -> None:
    """合并/删除账号后清掉缓存，避免拿旧编号继续查。"""
    _resolve.pop(int(account_pk), None)


def _status_cached() -> dict[str, Any]:
    if _status["value"] and _now() - float(_status["at"]) < _STATUS_TTL:
        return dict(_status["value"])
    from . import wechat4_live
    try:
        value = wechat4_live.status_cached()
    except Exception:                                # noqa: BLE001
        value = {"running": False, "storages": [], "databases": 0}
    _status.update(at=_now(), value=value)
    return dict(value)


def _display(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def sources(gh_id: str, channel: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """这台电脑上「能看到什么」的说明书。全部是毫秒级判断，不碰内存。"""
    from . import wechat4_live, wechat4_memory
    status = _status_cached()
    hint = {}
    try:
        hint = wechat4_live.session_hint(gh_id) or {}
    except Exception:                                # noqa: BLE001
        hint = {}
    try:
        memory_ok = bool(wechat4_memory.available())
    except Exception:                                # noqa: BLE001
        memory_ok = False
    try:
        in_contacts = bool(wechat4_live.known_account(gh_id))
    except Exception:                                # noqa: BLE001
        in_contacts = False
    newest = max([int(item.get("create_time") or 0) for item in (items or [])] or [0])
    days_since = None
    if newest:
        days_since = max(0, int((_now() - newest) / 86400))
    return {
        "channel": channel,
        "wechat_running": bool(status.get("running")),
        "wechat_pids": status.get("pids") or [],
        "memory_ready": memory_ok,
        "in_contacts": in_contacts,
        "db_synced_at": wechat4_live.manifest_synced_at(),
        "db_fingerprint": wechat4_live.fingerprint_hash(),
        "hint_at": int(hint.get("at") or 0),
        "hint_at_display": str(hint.get("at_display") or "") or _display(int(hint.get("at") or 0)),
        "hint_title": str(hint.get("title") or ""),
        "newest_item_at": newest,
        "newest_item_display": _display(newest),
        "days_since": days_since,
    }


def explain(state: str, src: dict[str, Any], note: str, total: int) -> str:
    """把「为什么是空的」写成一句人话。已经有更具体的 note 就不抢着说。"""
    if note:
        return note
    if state == "warming":
        return "正在读取本机微信，第一次会慢一点（扫描订阅号盒子需要几秒）。"
    if state == "error":
        return "这次没能读到本机微信。"
    if total:
        return ""
    if not src.get("wechat_running"):
        return ("微信现在没有运行，只能看到雷达已经归档的部分。"
                "打开微信后重新点开这个号，就能看到它最近的推文。")
    if src.get("in_contacts") is False:
        return ("本机微信的关注列表里没有这个号（可能已经取关，或者备注名和清单对不上），"
                "所以读不到它的推送。")
    return ("本机微信里还没有这个号的推送记录。微信只在收到推送或打开过该号会话时"
            "才把文章落盘，等它下次推送后再看。")


def peek(db: Any, account: dict[str, Any], limit: int = 24, offset: int = 0,
         days: int = 365, *, refresh: bool = False, allow_schedule: bool = True) -> dict[str, Any]:
    """详情页的即时响应：缓存 + 状态，最多等一两个毫秒级的查询。"""
    gh_id, channel = _gh_for(db, account)
    base: dict[str, Any] = {
        "account": {"id": account["id"], "name": account["name"], "account_id": gh_id,
                    "category_code": account.get("category_code"), "fit": account.get("fit"),
                    "enabled": int(account.get("enabled") or 0)},
        "total": 0, "offset": offset, "limit": limit, "has_more": False, "items": [],
        "channel": channel, "cached": False, "cache_age": 0, "synced_at": "",
        "refreshing": False, "state": "warming", "note": "", "sources": {},
    }
    if not gh_id:
        return {**base, "state": "error",
                "error": "没能把网页上的号名对到微信内部编号：微信里可能没关注它，或者备注名和清单不一致"}

    pk = int(account["id"])
    cache_row = wechat_panel.cache_state(db, account, gh_id)
    cache = cache_row["cache"]
    age = int(cache_row["age"]) if cache else 0
    if cache:
        page = wechat_panel._page(cache["items"], offset, limit, days)
        base.update({**page, "cached": True, "cache_age": age,
                     "synced_at": cache["synced_at"],
                     "note": str((cache["meta"] or {}).get("note") or "")})

    src = sources(gh_id, channel, base["items"])
    base["sources"] = src

    refreshing = is_refreshing(pk)
    has_any = bool(base["total"])
    # 本地这份解密副本太旧时，就算面板缓存没过期也要重读一次。worker 每 3 分钟
    # 会做一遍，这里只是兜底：worker 没起来、或者刚好卡在两次之间。
    from . import wechat4_live
    copy_stale = bool(src.get("wechat_running")) and wechat4_live.last_refresh_age() > 240
    wants = bool(refresh or not cache or not cache_row["fresh"] or copy_stale)
    started = False
    if wants and not refreshing and allow_schedule:
        started = schedule(db, account)
        refreshing = refreshing or started

    error = last_error(pk)
    # 只有「确实需要重读」的时候才把上次的失败摆出来。缓存还新鲜时翻出十分钟前
    # 的错误，会让人以为现在就是坏的。
    if error and not refreshing and (not cache or not cache_row["fresh"]):
        base["state"] = "error" if not has_any else "stale"
        base["note"] = f"读取本机微信失败：{error}"
        if has_any:
            base["note"] += f"；先显示 {max(1, age // 60)} 分钟前的缓存。"
    elif refreshing and not has_any:
        base["state"] = "warming"
    elif refreshing:
        base["state"] = "stale" if not cache_row["fresh"] else "ok"
    elif has_any:
        base["state"] = "ok"
    else:
        base["state"] = "empty"

    base["refreshing"] = refreshing
    base["note"] = explain(base["state"], src, base["note"], int(base["total"] or 0))
    return base
