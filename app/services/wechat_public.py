"""本机微信「公众号消息库」的读取层，只给公众号面板的「近期推文」用。

微信电脑版把订阅号收到的每条推送存进 `Msg/PublicMsg.db` 的 `PublicMsg` 表，
卡片 XML 里带标题、摘要、封面图和真正的发布时间。这里只读打开，不写雷达库、
不联网、不消耗任何接口额度——面板上看到的就是微信里真实收到的那一批。

两个必须记住的坑：

1. 按 `StrTalker` 查是全表扫描。表里有 36 万行且每页都要 SQLCipher 解密，
   实测一次 20 秒以上。按 `TalkerId` 查走 `MsgTalkerIdTypeSeqIndex`，1~3 毫秒。
   `TalkerId` 就是 `PublicNameToID` 的 rowid，所以先映射再查。
2. 封面和准确发布时间在 `appmsg/mmreader/category/item` 里，顶层 `<appmsg>`
   只有标题和链接。少数老消息没有 mmreader，那就退回顶层字段，别硬取。
"""
from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from ..utils import canonical_url
from .wechat_self_archive import (_account_and_key, _account_name_key, _decompress,
                                  _official_account_directory, _open_live_readonly)


class PublicMsgUnavailable(RuntimeError):
    """微信消息库现在读不到。消息写成人话，直接显示给用户。"""


_DIRECTORY_TTL = 300
_directory: dict[str, Any] = {"at": 0.0, "data": {}}
# 解析"当前是哪个微信、密钥是什么"要走 psutil 找进程 + 读 exe 版本 + 加载解密模块，
# 实测 1.7 秒。面板每翻一页都付这个代价太蠢，所以缓存住；读失败时立刻作废重来。
_RESOLVE_TTL = 120
_target_cache: dict[str, Any] = {"at": 0.0, "value": None}
_lock = threading.Lock()


def _target() -> tuple[Any, str]:
    with _lock:
        cached = _target_cache.get("value")
        if cached and time.time() - float(_target_cache.get("at") or 0) < _RESOLVE_TTL:
            return cached
    try:
        _, root, key = _account_and_key()
    except Exception as exc:                                  # noqa: BLE001 - 原因由底层给
        raise PublicMsgUnavailable(str(exc)) from exc
    path = root / "Msg" / "PublicMsg.db"
    if not path.is_file():
        raise PublicMsgUnavailable("本机微信还没有公众号消息库；在微信里打开一次「订阅号消息」再回来刷新")
    with _lock:
        _target_cache.update(at=time.time(), value=(path, key))
    return path, key


def _connect():
    """打开公众号消息库；任何一步失败都换成用户能看懂的一句话。"""
    path, key = _target()
    try:
        return _open_live_readonly(path, key)
    except Exception as exc:                                  # noqa: BLE001
        with _lock:
            _target_cache.update(at=0.0, value=None)
        raise PublicMsgUnavailable(f"读取微信公众号消息库失败：{exc}") from exc


def account_directory() -> dict[str, dict[str, Any]]:
    """gh_xxx → {name, aliases}。用来把网页上的号名对到微信内部编号。"""
    with _lock:
        if time.time() - float(_directory.get("at") or 0) < _DIRECTORY_TTL and _directory.get("data"):
            return _directory["data"]
    try:
        _, root, key = _account_and_key()
        data = _official_account_directory(root, key)
    except Exception:                                         # noqa: BLE001 - 没微信就只影响名字对齐
        return {}
    with _lock:
        _directory.update(at=time.time(), data=data)
    return data


def forget_directory() -> None:
    with _lock:
        _directory.update(at=0.0, data={})


def gh_id_for(name: str, account_id: str = "") -> str:
    """网页上的公众号 → 微信内部 gh_ 编号。

    `account_id` 里已经是 gh_ 开头就直接用；否则按备注名/昵称去联系人表里找。
    找不到同名时再放宽成互相包含，但只在名字够长（≥4 字）时才敢这么干，
    否则「生物」会把「合成生物学」整个吞掉。
    """
    known = (account_id or "").strip()
    if known.startswith("gh_"):
        return known
    target = _account_name_key(name)
    if not target:
        return ""
    directory = account_directory()
    for user, info in directory.items():
        if target in info["aliases"]:
            return user
    if len(target) < 4:
        return ""
    # 放宽成互相包含，但必须**唯一**命中：否则「Nature」会随手挑中「Nature Portfolio」，
    # 打开一个号却看到另一个号的推文，比找不到更糟。
    loose = [user for user, info in directory.items()
             if any(len(alias) >= 4 and (target in alias or alias in target) for alias in info["aliases"])]
    return loose[0] if len(loose) == 1 else ""


def parse_card(xml_text: str) -> dict[str, Any]:
    """把一条公众号卡片的 XML 拆成前端要的字段。解析不了就返回空字典。"""
    if not xml_text or "<appmsg" not in xml_text:
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    appmsg = root.find("appmsg")
    if appmsg is None:
        return {}

    def text(node: ET.Element | None, tag: str) -> str:
        if node is None:
            return ""
        return (node.findtext(tag) or "").strip()

    item = appmsg.find("mmreader/category/item")
    node = item if item is not None else appmsg
    topnew = appmsg.find("mmreader/category/topnew")
    title = text(node, "title") or text(appmsg, "title")
    url = text(node, "url") or text(appmsg, "url")
    summary = text(node, "summary") or text(node, "digest") or text(appmsg, "des")
    cover = text(node, "cover") or text(topnew, "cover")
    published = text(node, "pub_time")
    source_name = text(appmsg, "mmreader/category/name")
    epoch = int(published) if published.isdigit() else 0
    return {"title": title, "url": url, "summary": summary, "cover": cover,
            "published_epoch": epoch, "source_name": source_name}


def _talker_id(conn: Any, gh_id: str) -> int | None:
    row = conn.execute("SELECT rowid FROM PublicNameToID WHERE UsrName=?", (gh_id,)).fetchone()
    return int(row[0]) if row else None


def recent_articles(gh_id: str, limit: int = 24, offset: int = 0, days: int = 365) -> dict[str, Any]:
    """某个号最近的推文，按时间倒序。只读，不落库。"""
    limit = min(60, max(1, int(limit or 24)))
    offset = max(0, int(offset or 0))
    days = min(3650, max(1, int(days or 365)))
    conn = _connect()
    try:
        talker = _talker_id(conn, gh_id)
        if talker is None:
            return {"items": [], "total": 0, "has_more": False,
                    "note": "本机微信还没收到过这个号的消息"}
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        where = "TalkerId=? AND Type=49 AND SubType=5 AND CreateTime>=?"
        total = int(conn.execute(f"SELECT COUNT(*) FROM PublicMsg WHERE {where}", (talker, since)).fetchone()[0])
        rows = conn.execute(f"""SELECT localId,CreateTime,CompressContent,StrContent FROM PublicMsg
            WHERE {where} ORDER BY localId DESC LIMIT ? OFFSET ?""",
                            (talker, since, limit, offset)).fetchall()
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for local_id, created, compressed, plain in rows:
            card = parse_card(_decompress(compressed) or (plain if str(plain or "").lstrip().startswith("<") else ""))
            url = card.get("url") or ""
            if not url:
                continue
            try:
                canonical = canonical_url(url)
            except ValueError:
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            stamp = card.get("published_epoch") or int(created or 0)
            items.append({
                "canonical_url": canonical,
                "url": url,
                "title": card.get("title") or "(无标题)",
                "summary": (card.get("summary") or "")[:160],
                "cover": card.get("cover") or "",
                "published_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds"),
                "message_id": int(local_id),
            })
        return {"items": items, "total": total, "has_more": offset + len(rows) < total}
    finally:
        conn.close()
