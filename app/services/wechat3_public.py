"""微信 3.x 公众号库（PublicMsg.db）离线只读通道。

为什么需要它
------------
4.x 客户端把公众号消息写进 ``db_storage/message/biz_message_*.db``，但**只在
用户真的打开「订阅号消息」时才拉一批**。实测本机升级完成后 4.5 小时里，那个
目录一个字节都没变过 —— 同一时间 ``message_fts`` / ``contact`` / ``session``
都在写，说明通道是活的，只是没内容可落盘。

而 3.x 时代那个 ``PublicMsg.db`` 一条没丢：本机 36.6 万条、跨 2023-04 到
升级前最后一分钟。它才是"历史推文"的真实来源。所以 4.x 模式下公众号必须
**两路一起读**：

* ``biz_message_*`` —— 升级之后客户端真正收到的那一点点
* ``PublicMsg.db``  —— 升级之前的全部历史

两边加密参数完全不同
--------------------
4.x 是 SQLCipher 4（PBKDF2-SHA512 256000 轮 / HMAC-SHA512 / reserve 80），
3.x 是 SQLCipher 3（PBKDF2-SHA1 64000 轮 / HMAC-SHA1 / reserve 48）。3.x 的
密钥还是当年 ``wechat_db_key`` 存下来的那一把，能直接复用。

全程只读：只读打开微信的库，解密产物写在自己的 ``data/wechat3`` 里，绝不写
微信任何文件。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from ..paths import DATA_DIR

CACHE_DIR = DATA_DIR / "wechat3"
PAGE_SZ = 4096
KDF_ITER = 64000
KEY_SZ = 32
RESERVE_SZ = 48
SQLITE_HDR = b"SQLite format 3\x00"
CACHE_KEEP = 2


# --------------------------------------------------------------------------- #
# 1. 找到库
# --------------------------------------------------------------------------- #
def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def roots() -> list[Path]:
    """3.x 数据根目录的候选。

    升级到 4.x 之后 ``Documents\\WeChat Files`` 往往变成一条指向别的盘的
    目录链接（本机就指向 ``E:\\电脑管家迁移文件\\微信聊天记录搬家``）。``Path``
    进出都跟着链接走，所以这里不用区分真假目录。
    """
    found: list[Path] = []
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as key:
                value, _ = winreg.QueryValueEx(key, "FileSavePath")
                text = str(value or "").strip()
                # 3.x 的"我的文档"是个占位值，不是真路径
                if text and not text.lower().startswith("mydocument"):
                    found.append(Path(os.path.expandvars(text)))
        except OSError:
            pass
    found.append(Path.home() / "Documents" / "WeChat Files")
    result: list[Path] = []
    seen: set[str] = set()
    for path in found:
        identity = str(path).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        result.append(path)
    return result


def databases() -> list[Path]:
    """所有 ``<root>/<wxid>/Msg/PublicMsg.db``。"""
    found: list[Path] = []
    for root in roots():
        try:
            for path in root.glob("*/Msg/PublicMsg.db"):
                try:
                    if path.is_file() and path.stat().st_size > PAGE_SZ:
                        found.append(path)
                except OSError:
                    continue
        except OSError:
            continue
    return found


def pick() -> dict[str, Any] | None:
    """挑内容最新的那个库。

    一个安装里可能留着好几个历史账号的库（换过号、重装过），只有最后在用的
    那个 mtime 最近 —— 拿错账号会得到一份"别人的"公众号列表。
    """
    best: dict[str, Any] | None = None
    for path in databases():
        try:
            stat = path.stat()
        except OSError:
            continue
        stamp = max(stat.st_mtime, _mtime(path.with_name(path.name + "-wal")))
        if best is None or stamp > best["mtime"]:
            best = {"path": path, "size": stat.st_size, "mtime": stamp}
    return best


# --------------------------------------------------------------------------- #
# 2. 解密（SQLCipher 3）
# --------------------------------------------------------------------------- #
def _derive(key_hex: str, salt: bytes) -> tuple[bytes, bytes]:
    password = bytes.fromhex(key_hex.strip())
    enc_key = hashlib.pbkdf2_hmac("sha1", password, salt, KDF_ITER, KEY_SZ)
    mac_salt = bytes(value ^ 58 for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha1", enc_key, mac_salt, 2, KEY_SZ)
    return enc_key, mac_key


def key_ok(db_path: Path, key_hex: str) -> bool:
    """用第一页的 HMAC 验一次密钥，不要靠"能不能打开"来猜。"""
    if len(key_hex or "") != 64:
        return False
    try:
        with db_path.open("rb") as handle:
            page = handle.read(PAGE_SZ)
    except OSError:
        return False
    if len(page) != PAGE_SZ or page[:16] == SQLITE_HDR[:16]:
        return False
    try:
        _, mac_key = _derive(key_hex, page[:16])
    except ValueError:
        return False
    digest = hmac.new(mac_key, page[16:-32], hashlib.sha1)
    digest.update(b"\x01\x00\x00\x00")
    return hmac.compare_digest(digest.digest(), page[-32:-12])


def fingerprint(db_path: Path) -> str:
    """库的"形状"指纹：主库和 -wal 的大小 + 纳秒 mtime。"""
    parts: list[str] = []
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.extend([str(stat.st_size), str(stat.st_mtime_ns)])
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _decrypt_pages(db_path: Path, out_path: Path, key_hex: str) -> int:
    """逐页解密，不把 1.8 GB 全吃进内存。

    SQLCipher 3 的页布局：``[payload][IV 16][HMAC 20][padding 12]``，第一页
    最前面还有 16 字节 salt。payload 正好是 16 的整数倍，CBC 能直接走。
    """
    from Crypto.Cipher import AES  # pycryptodome

    with db_path.open("rb") as source:
        first = source.read(PAGE_SZ)
        if len(first) != PAGE_SZ:
            raise RuntimeError("PublicMsg.db 第一页读不全")
        enc_key, _ = _derive(key_hex, first[:16])
        temporary = out_path.with_suffix(".part")
        pages = 0
        with temporary.open("wb") as target:
            target.write(SQLITE_HDR)
            target.write(AES.new(enc_key, AES.MODE_CBC, first[-RESERVE_SZ:-32])
                         .decrypt(first[16:-RESERVE_SZ]))
            target.write(b"\x00" * RESERVE_SZ)
            pages = 1
            while True:
                page = source.read(PAGE_SZ)
                if len(page) < PAGE_SZ:
                    break
                target.write(AES.new(enc_key, AES.MODE_CBC, page[-RESERVE_SZ:-32])
                             .decrypt(page[:-RESERVE_SZ]))
                target.write(b"\x00" * RESERVE_SZ)
                pages += 1
        temporary.replace(out_path)
    return pages


def snapshot(db_path: Path | None = None, key_hex: str | None = None) -> dict[str, Any] | None:
    """把 3.x 库解到自己的缓存里；同一个指纹只解一次。

    返回 ``{"db": 解密后的路径, "source": 原库, "pages": 页数, "cached": bool}``；
    读不到密钥或密钥不对就返回 ``None``，调用方据此安静降级。
    """
    chosen = pick() if db_path is None else {"path": db_path, "size": 0, "mtime": 0}
    if not chosen:
        return None
    source = Path(chosen["path"])
    if key_hex is None:
        from ..settings import get_secret
        key_hex = get_secret("wechat_db_key") or ""
    if not key_ok(source, key_hex):
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = fingerprint(source)
    target = CACHE_DIR / f"PublicMsg-{stamp}.db"
    if target.is_file() and target.stat().st_size > PAGE_SZ:
        return {"db": target, "source": source, "pages": 0, "cached": True}

    pages = _decrypt_pages(source, target, key_hex)
    # 只留最近几份，别把 data 目录堆成仓库
    for old in sorted(CACHE_DIR.glob("PublicMsg-*.db"), key=_mtime, reverse=True)[CACHE_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return {"db": target, "source": source, "pages": pages, "cached": False}


# --------------------------------------------------------------------------- #
# 3. 读文章
# --------------------------------------------------------------------------- #
def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _lz4(data: Any) -> str:
    if not data:
        return ""
    try:
        import lz4.block
    except Exception:                                    # noqa: BLE001
        return ""
    try:
        try:
            decoded = lz4.block.decompress(data)
        except lz4.block.LZ4BlockError:
            decoded = lz4.block.decompress(data, uncompressed_size=len(data) << 10)
        return decoded.decode("utf-8", errors="ignore").replace("\x00", "")
    except Exception:                                    # noqa: BLE001
        return ""


def _mid_idx(url: str) -> tuple[str, int]:
    """从链接里取 (mid, idx)：3.x 和 4.x 的链接都带这两个参数。"""
    mid = re.search(r"[?&]mid=(\d+)", url)
    idx = re.search(r"[?&]idx=(\d+)", url)
    return (mid.group(1) if mid else ""), int(idx.group(1)) if idx else 1


def _url_rank(url: str) -> tuple:
    """同一篇在 XML 里可能出现两份，挑参数更全的那条。"""
    return ("\ufffd" not in url, url.startswith("https://"),
            "chksm=" in url, "sn=" in url, -len(url))


def cards(content: Any, fallback: str = "") -> list[dict[str, str]]:
    """把一条 Type=49/SubType=5 的卡片解成**若干篇** title/des/url/account。

    ``CompressContent`` 是 lz4；老消息可能直接躺在 ``StrContent`` 里，所以
    两条路都要试一遍。

    一次群发多篇时，微信 3.x 把它们存成**一条** PublicMsg 行，XML 里并排几个
    ``<appmsg>``。只取第一个就等于把靠下的几篇整条丢掉——2026-11-05 黄硕课题组
    那批（环肽 + JACS 位阻标签）就是这么漏的。这里全部展开，并按 (mid,idx) 去重
    （同一篇在 XML 里常常有两份，参数完整度不同）。
    """
    xml = _lz4(content) or (fallback if str(fallback or "").lstrip().startswith("<") else "")
    if not xml:
        return []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    import html as html_module

    def text(node: Any, tag: str) -> str:
        return html_module.unescape(node.findtext(tag) or "").strip()

    picked: dict[str, dict[str, str]] = {}
    for appmsg in root.iter("appmsg"):
        # 有的群发把几篇塞进 <mmreader><category><item> 列表里，而不是并排 <appmsg>
        nodes = appmsg.findall("mmreader/category/item") or [appmsg]
        for node in nodes:
            item = {"title": text(node, "title") or text(appmsg, "title"),
                    "des": text(node, "summary") or text(node, "digest") or text(appmsg, "des"),
                    "url": text(node, "url") or text(appmsg, "url"),
                    "account": text(appmsg, "sourcedisplayname") or text(appmsg, "nickname")}
            if not (item["url"] or item["title"]):
                continue
            mid, idx = _mid_idx(item["url"])
            item["idx"] = str(idx)
            key = f"{mid}#{idx}" if mid else item["url"].split("#", 1)[0]
            prev = picked.get(key)
            if prev is None or _url_rank(item["url"]) > _url_rank(prev["url"]):
                picked[key] = item
    return list(picked.values())


def card(content: Any, fallback: str = "") -> dict[str, str]:
    """兼容老调用：只要第一篇。多篇场景请用 :func:`cards`。"""
    found = cards(content, fallback)
    return found[0] if found else {}


def articles(since_ts: int | None = None, after_local_id: int = 0,
             limit: int = 400, snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """按 localId 增量取公众号文章卡片。

    ``localId`` 是 3.x 的自增主键，比时间戳可靠：同一秒里发好几篇也不会漏。
    """
    state = snap or snapshot()
    if not state:
        return []
    conditions = ["Type=49", "SubType=5", "localId>?"]
    params: list[Any] = [int(after_local_id)]
    if since_ts:
        conditions.append("CreateTime>=?")
        params.append(int(since_ts))
    params.append(max(1, int(limit)))
    sql = (f"SELECT localId,CreateTime,StrTalker,StrContent,CompressContent,DisplayContent "
           f"FROM PublicMsg WHERE {' AND '.join(conditions)} ORDER BY localId LIMIT ?")
    con = _connect(Path(state["db"]))
    try:
        rows = []
        for row in con.execute(sql, params):
            for info in cards(row["CompressContent"],
                              row["StrContent"] or row["DisplayContent"] or ""):
                url = info.get("url") or ""
                if "mp.weixin.qq.com" not in url:
                    continue
                rows.append({
                    "user_name": str(row["StrTalker"] or ""),
                    "local_id": int(row["localId"]),
                    "create_time": int(row["CreateTime"] or 0),
                    "title": info.get("title") or url,
                    "digest": info.get("des", ""),
                    "url": url,
                    "account": info.get("account", ""),
                    # 同一行里的第几篇：入库键要靠它区分，否则只留得下第一篇
                    "idx": int(info.get("idx") or 1),
                })
                if len(rows) >= max(1, int(limit)):
                    return rows
        return rows
    finally:
        con.close()


def recent_for(username: str, limit: int = 60, days: int = 365,
               snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """某一个公众号在 3.x 库里的文章卡片，字段与 4.x 的 ``recent_for`` 对齐。

    面板上点开一个号时，4.x 那边往往只有零到两条，真正的历史在这里。字段名必须
    和 ``wechat4_live.recent_for`` 一模一样，调用方才能把两路直接拼起来。
    """
    state = snap or snapshot()
    if not state:
        return []
    since = int(time.time() - max(1, int(days)) * 86400) if days else None
    conditions = ["StrTalker=?", "Type=49", "SubType=5"]
    params: list[Any] = [str(username)]
    if since:
        conditions.append("CreateTime>=?")
        params.append(since)
    params.append(max(1, int(limit)) * 3)       # 多取一些，非 mp 链接和重复会筛掉
    sql = (f"SELECT localId,CreateTime,StrTalker,StrContent,CompressContent,DisplayContent "
           f"FROM PublicMsg WHERE {' AND '.join(conditions)} "
           f"ORDER BY CreateTime DESC LIMIT ?")
    con = _connect(Path(state["db"]))
    try:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in con.execute(sql, params):
            for info in cards(row["CompressContent"],
                              row["StrContent"] or row["DisplayContent"] or ""):
                url = info.get("url") or ""
                if "mp.weixin.qq.com" not in url:
                    continue
                from ..utils import canonical_url
                canonical = canonical_url(url)
                if canonical in seen:
                    continue
                seen.add(canonical)
                out.append({"create_time": int(row["CreateTime"] or 0),
                            "local_id": int(row["localId"]),
                            "idx": int(info.get("idx") or 1),
                            "title": info.get("title", ""),
                            "url": url, "canonical_url": canonical,
                            "digest": info.get("des", ""), "visits": ""})
                if len(out) >= max(1, int(limit)):
                    return out
        return out
    finally:
        con.close()


def watermark(snap: dict[str, Any] | None = None) -> int:
    """库里最大的 localId —— 首次接上时拿它当起点，避免把三年历史一次灌进去。"""
    state = snap or snapshot()
    if not state:
        return 0
    con = _connect(Path(state["db"]))
    try:
        row = con.execute("SELECT COALESCE(max(localId),0) FROM PublicMsg").fetchone()
        return int(row[0] or 0)
    finally:
        con.close()


def stats(snap: dict[str, Any] | None = None) -> dict[str, Any]:
    """库的整体情况，给体检面板用。"""
    state = snap or snapshot()
    if not state:
        return {"ok": False, "reason": "读不到 3.x 公众号库或密钥"}
    con = _connect(Path(state["db"]))
    try:
        total, low, high = con.execute(
            "SELECT count(*), min(CreateTime), max(CreateTime) FROM PublicMsg").fetchone()
        accounts = con.execute(
            "SELECT count(DISTINCT StrTalker) FROM PublicMsg WHERE StrTalker LIKE 'gh_%'").fetchone()[0]
        return {"ok": True, "total": int(total or 0), "newest": int(high or 0),
                "oldest": int(low or 0), "accounts": int(accounts or 0),
                "source": str(state["source"]), "cached": state["cached"]}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 4. 号名
# --------------------------------------------------------------------------- #
def directory(contact_db: Path | None = None) -> dict[str, dict[str, Any]]:
    """``gh_xxx -> {"name", "aliases"}``。

    优先用 4.x 的 ``contact.db``：升级时通讯录是完整带过来的（本机 1714 个
    公众号全有昵称），比再去解一个 3.x 的 MicroMsg.db 便宜得多。
    """
    result: dict[str, dict[str, Any]] = {}
    candidates: list[Path] = []
    if contact_db is not None:
        candidates.append(contact_db)
    else:
        try:
            from .wechat4_live import DEFAULT_CACHE
            for path in sorted((DEFAULT_CACHE / "contact").glob("contact*.db")):
                candidates.append(path)
        except Exception:                                # noqa: BLE001
            pass
    for path in candidates:
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "contact" not in tables:
                continue
            columns = {row[1] for row in con.execute("PRAGMA table_info(contact)")}
            nick = "nick_name" if "nick_name" in columns else "nickname"
            for username, nickname, remark in con.execute(
                    f"SELECT username,{nick},remark FROM contact WHERE username LIKE 'gh_%'"):
                name = str(remark or nickname or username or "")
                if not name or name in {"未命名公众号"}:
                    name = str(nickname or username or "")
                aliases = {_name_key(v) for v in (nickname, remark) if str(v or "").strip()}
                result[str(username)] = {"name": name, "aliases": aliases}
        except sqlite3.Error:
            continue
        finally:
            con.close()
        if result:
            break
    return result


def _name_key(value: str) -> str:
    import unicodedata
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())
