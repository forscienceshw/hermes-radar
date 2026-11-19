"""Read-only extraction of links from the current user's WeChat self-chat.

This adapts the open-source MemoTrace/WeChatMsg decryptor for the installed WeChat 3.x line.
Only the current account's own conversation is queried. No plaintext key is logged.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import html
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..paths import BASE_DIR, DATA_DIR, TMP_DIR
from ..settings import get_secret, set_secret
from ..utils import (canonical_url, epoch_iso, extract_urls, json_loads, name_match_key,
                     now_iso, parse_time)

VENDOR_DIR = BASE_DIR / "analysis" / "wechatmsg_full" / "app" / "decrypt"
ARCHIVE_DIR = DATA_DIR / "wechat-self"
ACCOUNT_FILE = ARCHIVE_DIR / "account.json"
_TALKER_CACHE: dict[tuple[str, str, str], str] = {}


@contextmanager
def _scratch_dir(prefix: str):
    """A scratch folder whose cleanup can never abort a sync.

    `TemporaryDirectory(ignore_cleanup_errors=True)` only swallows OSError.  A host
    whose file guard refuses a recursive delete raises SystemExit instead, and that
    escapes the context manager entirely.  Losing a temp copy is harmless; failing
    to read the user's messages over it is not.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=TMP_DIR))
    try:
        yield path
    finally:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except BaseException:          # noqa: BLE001 - cleanup must never mask the result
            pass


def _load_vendor_module(name: str):
    path = VENDOR_DIR / (name + ".py")
    if not path.is_file():
        raise RuntimeError("缺少 MemoTrace 解密模块，请检查 analysis/wechatmsg_full 目录")
    spec = importlib.util.spec_from_file_location("radar_wechat_" + name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _wechat_process():
    import psutil
    for process in psutil.process_iter(["name", "exe", "pid"]):
        if process.name().lower() == "wechat.exe":
            return process
    return None


def _file_version(path: str) -> str:
    from win32api import GetFileVersionInfo, HIWORD, LOWORD
    info = GetFileVersionInfo(path, "\\")
    msv, lsv = info["FileVersionMS"], info["FileVersionLS"]
    return f"{HIWORD(msv)}.{LOWORD(msv)}.{HIWORD(lsv)}.{LOWORD(lsv)}"


def _check_supported_version(process) -> str:
    version = _file_version(process.exe())
    version_file = VENDOR_DIR / "version_list.json"
    supported = json.loads(version_file.read_text(encoding="utf-8")) if version_file.exists() else {}
    if not version.startswith("3.") or version not in supported:
        raise RuntimeError(f"当前微信版本 {version} 尚未通过只读适配验证；4.x 必须单独适配，不能套用 3.x 解密方式")
    return version


def _key_is_valid(db_root: Path, key: str) -> bool:
    path = db_root / "Msg" / "MicroMsg.db"
    if len(key) != 64 or not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            header = handle.read(4096)
        if len(header) != 4096:
            return False
        salt = header[:16]
        password = bytes.fromhex(key)
        encryption_key = hashlib.pbkdf2_hmac("sha1", password, salt, 64000, 32)
        mac_salt = bytes(value ^ 58 for value in salt)
        mac_key = hashlib.pbkdf2_hmac("sha1", encryption_key, mac_salt, 2, 32)
        digest = hmac.new(mac_key, header[16:-32], hashlib.sha1)
        digest.update(b"\x01\x00\x00\x00")
        return hmac.compare_digest(digest.digest(), header[-32:-12])
    except (OSError, ValueError):
        return False


def _save_account(wxid: str, db_root: Path, version: str) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = ACCOUNT_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"wxid": wxid, "db_root": str(db_root), "wechat_version": version},
                                    ensure_ascii=False), encoding="utf-8")
    temporary.replace(ACCOUNT_FILE)


def _offline_root_candidates(wxid: str, saved_root: str) -> list[Path]:
    """Locate only known WeChat data locations; never scan whole personal drives."""
    candidates = [Path(saved_root)] if saved_root else []
    bases: list[Path] = []
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as key:
                value, _ = winreg.QueryValueEx(key, "FileSavePath")
                if value:
                    bases.append(Path(os.path.expandvars(str(value))))
        except OSError:
            pass
        documents = Path.home() / "Documents" / "WeChat Files"
        bases.append(documents)
    for base in bases:
        candidates.extend([base, base / wxid, base / "WeChat Files" / wxid])
    if saved_root:
        old = Path(saved_root)
        candidates.append(old.parent / wxid)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = str(candidate).casefold()
        if identity not in seen:
            seen.add(identity)
            result.append(candidate)
    return result


def _locate_offline_root(wxid: str, saved_root: str, key: str) -> Path | None:
    for candidate in _offline_root_candidates(wxid, saved_root):
        if candidate.is_dir() and _key_is_valid(candidate, key):
            return candidate
    return None


def _account_and_key() -> tuple[str, Path, str]:
    saved = json_loads(ACCOUNT_FILE.read_text(encoding="utf-8"), {}) if ACCOUNT_FILE.exists() else {}
    saved_key = get_secret("wechat_db_key")
    process = _wechat_process()
    if process is None:
        import psutil
        if any((item.name() or "").lower() == "weixin.exe" for item in psutil.process_iter(["name"])):
            raise RuntimeError("检测到微信 4.x 的 Weixin.exe；当前只读适配器不会复用 3.x 密钥读取新版本数据")
        if saved and saved_key:
            if not str(saved.get("wechat_version", "")).startswith("3."):
                raise RuntimeError("缺少已验证的 3.x 版本标记；请登录当前微信并重新验证，不会默认读取旧数据")
            db_root = _locate_offline_root(saved["wxid"], saved.get("db_root", ""), saved_key)
            if db_root:
                if str(db_root) != saved.get("db_root"):
                    _save_account(saved["wxid"], db_root, saved["wechat_version"])
                return saved["wxid"], db_root, saved_key
            raise RuntimeError("旧数据目录已失效，且未从微信配置中找到可验证的新目录；已存资料不受影响，登录微信后会重新定位")
        raise RuntimeError("微信电脑版未运行；首次同步需要登录并保持微信运行")

    _check_supported_version(process)
    vendor = _load_vendor_module("get_wx_info")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0410, False, process.pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        error = ctypes.get_last_error()
        if error == 5:
            raise PermissionError("Windows 拒绝读取当前微信进程（访问被拒绝）；请用以管理员身份运行的雷达同步器执行")
        raise PermissionError(f"无法读取当前微信进程（Windows 错误 {error}）")
    try:
        wxid = vendor.get_info_wxid(handle)
    except Exception as exc:
        raise RuntimeError(f"读取当前账号标识失败（{type(exc).__name__}）；请确认微信已登录且版本未变化") from None
    finally:
        kernel32.CloseHandle(handle)
    if not wxid or wxid == "None":
        raise RuntimeError("未能识别当前微信账号，请先打开并登录微信")
    db_root = Path(vendor.get_info_filePath(wxid))
    if not db_root.is_dir():
        raise RuntimeError("当前账号的数据库目录不存在")

    version = _file_version(process.exe())
    if saved.get("wxid") == wxid and saved_key and _key_is_valid(db_root, saved_key):
        if str(db_root) != saved.get("db_root") or version != saved.get("wechat_version"):
            _save_account(wxid, db_root, version)
        return wxid, db_root, saved_key
    pointer_size = vendor.get_exe_bit(process.exe()) // 8
    try:
        key = vendor.get_key(str(db_root), pointer_size)
    except Exception as exc:
        raise RuntimeError(f"数据库密钥扫描失败（{type(exc).__name__}）；请用管理员权限运行并确认微信已登录") from None
    if not key or key == "None" or not _key_is_valid(db_root, key):
        raise RuntimeError("未能取得可验证的数据库密钥；请保持微信已登录并打开过自己的聊天窗口")
    set_secret("wechat_db_key", key)
    _save_account(wxid, db_root, version)
    return wxid, db_root, key


def _decompress(data: bytes | None) -> str:
    if not data:
        return ""
    import lz4.block
    try:
        try:
            decoded = lz4.block.decompress(data)
        except lz4.block.LZ4BlockError:
            decoded = lz4.block.decompress(data, uncompressed_size=len(data) << 10)
        return decoded.decode("utf-8", errors="ignore").replace("\x00", "")
    except (lz4.block.LZ4BlockError, ValueError):
        return ""


def _card(content: bytes | None, fallback: str = "") -> tuple[str, str, str, str]:
    xml = _decompress(content) or (fallback if fallback.lstrip().startswith("<") else "")
    if not xml:
        return "", "", "", ""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        appmsg = root.find("appmsg")
        if appmsg is None:
            return "", "", "", ""
        title, description, url = (html.unescape(appmsg.findtext(tag) or "") for tag in ("title", "des", "url"))
        return title, description, url, (appmsg.findtext("type") or "").strip()
    except ET.ParseError:
        return "", "", "", ""


def _database_signature(source: Path) -> dict[str, int]:
    stat = source.stat()
    signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    wal = Path(str(source) + "-wal")
    if wal.exists():
        wal_stat = wal.stat()
        signature.update({"wal_size": wal_stat.st_size, "wal_mtime_ns": wal_stat.st_mtime_ns})
    return signature


def _signature_unchanged(previous: Any, signature: dict[str, int]) -> bool:
    """Compare only the file-shape keys; the cursor also stores a message watermark."""
    if not isinstance(previous, dict):
        return False
    return all(previous.get(key) == value for key, value in signature.items())


def _open_snapshot(source: Path, target: Path, master_key: str):
    """Copy encrypted data and sidecars, then read only the temporary SQLCipher snapshot."""
    import shutil
    from sqlcipher3 import dbapi2 as sqlcipher
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(target) + suffix))
    with target.open("rb") as handle:
        salt = handle.read(16)
    if len(salt) != 16:
        raise RuntimeError("数据库快照缺少有效盐值")
    page_key = hashlib.pbkdf2_hmac("sha1", bytes.fromhex(master_key), salt, 64000, 32).hex()
    conn = sqlcipher.connect(str(target))
    try:
        conn.execute(f"PRAGMA key = \"x'{page_key}'\"")
        conn.execute("PRAGMA cipher_compatibility = 3")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except Exception:
        conn.close()
        raise


def _open_live_readonly(source: Path, master_key: str):
    """Open the active database in SQLite read-only mode so WAL is handled without a full copy."""
    from sqlcipher3 import dbapi2 as sqlcipher
    with source.open("rb") as handle:
        salt = handle.read(16)
    if len(salt) != 16:
        raise RuntimeError("当前数据库缺少有效盐值")
    page_key = hashlib.pbkdf2_hmac("sha1", bytes.fromhex(master_key), salt, 64000, 32).hex()
    conn = sqlcipher.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute(f"PRAGMA key = \"x'{page_key}'\"")
        conn.execute("PRAGMA cipher_compatibility = 3")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except Exception:
        conn.close()
        raise


def _resolve_talker(root: Path, key: str, requested: str, target_name: str = "") -> str:
    """Resolve a displayed WeChat alias/remark to the single internal conversation ID."""
    requested = requested.strip()
    cache_key = (str(root), requested, target_name.strip())
    if cache_key in _TALKER_CACHE:
        return _TALKER_CACHE[cache_key]
    source = root / "Msg" / "MicroMsg.db"
    if not source.is_file():
        raise RuntimeError("当前账号缺少联系人数据库，无法解析指定微信号")
    with _scratch_dir("contact-") as temporary:
        conn = _open_snapshot(source, Path(temporary) / source.name, key)
        try:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute("""SELECT DISTINCT UserName FROM Contact
                WHERE UserName=? COLLATE NOCASE OR Alias=? COLLATE NOCASE
                OR (? <> '' AND (NickName=? COLLATE NOCASE OR Remark=? COLLATE NOCASE))""",
                (requested, requested, target_name.strip(), target_name.strip(), target_name.strip())).fetchall()
        finally:
            conn.close()
    if len(rows) != 1:
        raise RuntimeError(f"指定聊天窗口匹配到 {len(rows)} 个联系人；请使用准确的微信号或唯一窗口名称")
    talker = rows[0][0]
    _TALKER_CACHE[cache_key] = talker
    return talker


def sync_self_chat(previous_cursor: dict[str, Any] | None = None, target_wxid: str = "",
                   target_name: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read only one explicitly selected conversation from the current WeChat databases."""
    from sqlcipher3 import dbapi2 as sqlcipher
    current_wxid, root, key = _account_and_key()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    talker = target_wxid.strip() or current_wxid
    if target_wxid.strip() and target_wxid.strip() != current_wxid:
        talker = _resolve_talker(root, key, target_wxid, target_name)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    conversation_key = hashlib.sha256(("wechat-chat:" + talker).encode("utf-8")).hexdigest()[:16]
    previous_files = (previous_cursor or {}).get("files", {}) if (previous_cursor or {}).get("conversation_key") == conversation_key else {}
    if not isinstance(previous_files, dict):
        previous_files = {}
    files_cursor: dict[str, Any] = {}
    warnings: list[str] = []
    scanned = unchanged = succeeded = 0
    database_files = sorted((root / "Msg" / "Multi").glob("MSG*.db"))
    chat_file = root / "Msg" / "ChatMsg.db"
    if chat_file.exists():
        database_files.append(chat_file)
    if not database_files:
        raise RuntimeError("未找到当前账号的消息数据库；请确认微信至少登录并完成过一次消息同步")

    # ignore_cleanup_errors: on hosts whose file guard refuses a recursive delete the
    # scratch copy can stay behind.  Losing a temp folder is harmless; aborting a
    # message sync because cleanup failed is not.
    with _scratch_dir("readonly-") as temporary:
        for source in database_files:
            relative = source.relative_to(root).as_posix()
            signature = _database_signature(source)
            previous_entry = previous_files.get(relative) if isinstance(previous_files.get(relative), dict) else {}
            if _signature_unchanged(previous_entry, signature) and "max_local_id" in previous_entry:
                files_cursor[relative] = previous_entry
                unchanged += 1
                continue
            # Each shard is its own SQLite file, so localId only increases inside one shard.
            # Keep a per-shard watermark: without it a single new message re-reports every
            # historical link in that conversation as a "discovery".
            watermark = int(previous_entry.get("max_local_id") or 0)
            if signature["size"] < 4096:
                warnings.append(f"{source.name} 尚无完整数据库页，已跳过")
                continue
            scanned += 1
            target = Path(temporary) / source.name
            try:
                conn = _open_snapshot(source, target, key)
                conn.execute("PRAGMA query_only=ON")
                try:
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='MSG'").fetchone()
                    if exists:
                        highest = int(conn.execute("SELECT COALESCE(max(localId),0) FROM MSG").fetchone()[0])
                        if watermark > highest:
                            # A restored/compacted database can renumber rows; restart this shard.
                            watermark = 0
                        rows = conn.execute("""SELECT localId,CreateTime,Type,SubType,StrContent,CompressContent
                            FROM MSG WHERE StrTalker=? AND (Type=49 OR Type=1) AND localId>?
                            ORDER BY CreateTime,localId""", (talker, watermark)).fetchall()
                        for local_id, created, msg_type, subtype, text, compressed in rows:
                            urls: list[str] = []
                            title = description = ""
                            if msg_type == 49:
                                if subtype == 57:  # 引用消息，不是普通链接分享卡片
                                    continue
                                title, description, card_url, _ = _card(compressed, str(text or ""))
                                if card_url:
                                    urls = [card_url]
                            elif msg_type == 1:
                                urls = extract_urls(str(text or ""))
                            for url in urls:
                                if not url.lower().startswith(("http://", "https://")):
                                    continue
                                try:
                                    canonical = canonical_url(url)
                                    host = (urlsplit(url).hostname or "").lower()
                                except ValueError:
                                    continue
                                kind = "video" if host == "b23.tv" or host.endswith(".bilibili.com") or host == "bilibili.com" else "article"
                                identity = (kind, canonical)
                                if identity in seen:
                                    continue
                                seen.add(identity)
                                candidates.append({
                                    "kind": kind, "url": url, "canonical_url": canonical,
                                    "external_key": f"chat:{source.name}:{local_id}:{canonical}",
                                    "title": title or url, "author": "当前微信指定聊天窗口",
                                    "published_at": epoch_iso(created),
                                    "metadata": {"share_channel": "wechat_self_chat" if talker == current_wxid else "wechat_current_chat",
                                                 "message_id": local_id, "description": description},
                                })
                    succeeded += 1
                    new_watermark = max([watermark] + [int(row[0]) for row in rows])
                    entry = dict(signature)
                    entry["max_local_id"] = new_watermark
                    files_cursor[relative] = entry
                    if _database_signature(source) != signature:
                        # The shard grew while we were reading; the watermark still holds,
                        # so the next pass starts from here instead of rescanning history.
                        warnings.append(f"{source.name} 在读取期间发生更新，已记录读取进度")
                finally:
                    conn.close()
            except (OSError, sqlite3.Error, RuntimeError, sqlcipher.Error) as exc:
                warnings.append(f"{source.name} 本次只读快照失败（{type(exc).__name__}），将在下次同步重试")

    if scanned and not succeeded:
        raise RuntimeError("所有待扫描消息数据库均解密或校验失败；请检查微信版本、管理员权限及账号是否已切换")
    cursor = {"files": files_cursor, "conversation_key": conversation_key, "last_read_at": now_iso(),
              "scanned_databases": scanned, "unchanged_databases": unchanged,
              "matched_links": len(candidates), "warnings": warnings}
    return candidates, cursor


def read_self_chat_links() -> list[dict[str, Any]]:
    """One-shot full read for a local validation; results contain no other conversations."""
    candidates, _ = sync_self_chat()
    return candidates


# --------------------------------------------------------------------------- #
# 3.x 那条路依赖 WeChat Files\Msg\*.db；4.x 的库在 xwechat_files\<账号>\db_storage，
# 按会话分表、正文 zstd 压缩、加密也换了参数。两边只能各走各的，所以在入口处分派。
def _wechat3_root() -> Path:
    return Path.home() / "Documents" / "WeChat Files"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _wechat3_present() -> bool:
    """3.x 的数据目录还在。注意：升级到 4.x 之后它**仍然在**，不能拿它当"用 3.x"的证据。"""
    root = _wechat3_root()
    try:
        return root.is_dir() and any(root.iterdir())
    except OSError:
        return False


def _wechat3_freshness() -> float:
    """3.x 公众号库最后一次被写入的时间；读不到就是 0。

    3.x 的库在 `WeChat Files\\<wxid>\\Msg\\PublicMsg.db`，不是根目录下 ——
    少一层 wxid 就会永远算出 0，进而把通道判定带偏。
    """
    root = _wechat3_root()
    newest = 0.0
    for database in root.glob("*/Msg/PublicMsg.db"):
        newest = max(newest, _mtime(database), _mtime(database.with_name(database.name + "-wal")))
    return newest


def _wechat4_freshness() -> float:
    try:
        from . import wechat4_live
    except Exception:                                # noqa: BLE001
        return 0.0
    cache = getattr(wechat4_live, "DEFAULT_CACHE", None)
    if not cache:
        return 0.0
    newest = 0.0
    for path in wechat4_live.message_dbs(cache, "message/biz_message_*.db"):
        newest = max(newest, _mtime(path), _mtime(path.with_name(path.name + "-wal")))
    return newest


def local_channel() -> str:
    """这台机器现在该读哪个版本的微信：`"4.x"` / `"3.x"` / `""`。

    只看"目录在不在"会踩坑：升级到 4.x 之后，3.x 的 `Documents\\WeChat Files`
    仍然在（本机还是一条指向别的盘的软链接），于是每次都被分派去 3.x，去读一份
    再也不会更新的库 —— 体检显示"已就绪"，新推文却一篇都进不来。判断顺序因此改成：

    1. 4.x 的 Weixin.exe 在跑 → 只能是 4.x（3.x 的读取器这时会直接拒绝）；
    2. 只有一边有数据 → 那一边；
    3. 两边都有 → 比公众号库的最后写入时间，谁新用谁。

    调试/测试可以用 `RADAR_WECHAT_CHANNEL=3.x|4.x` 强制指定 —— 单元测试不该被
    这台机器上装的是哪一代微信影响。
    """
    forced = str(os.environ.get("RADAR_WECHAT_CHANNEL") or "").strip().lower()
    if forced in {"3.x", "4.x"}:
        return forced
    four_ok = False
    four_running = False
    try:
        from . import wechat4_live
        four_running = bool(wechat4_live.quick_status().get("running"))
        four_ok = wechat4_live.available()
    except Exception:                                # noqa: BLE001
        four_ok = False
    three_ok = _wechat3_present()
    if four_running and four_ok:
        return "4.x"
    if four_ok and not three_ok:
        return "4.x"
    if three_ok and not four_ok:
        return "3.x"
    if not four_ok and not three_ok:
        return ""
    return "4.x" if _wechat4_freshness() >= _wechat3_freshness() else "3.x"


def _wechat4():
    """本机有 4.x 且（在跑 或 有解密缓存）时返回通道模块，否则 None。"""
    try:
        from . import wechat4_live
    except Exception:                                # noqa: BLE001
        return None
    try:
        return wechat4_live if wechat4_live.available() else None
    except Exception:                                # noqa: BLE001
        return None


def _wechat4_sync(module) -> dict[str, Any]:
    """刷新解密缓存，返回给游标里留痕的同步摘要。"""
    try:
        state = module.refresh()
    except Exception as exc:                         # noqa: BLE001
        return {"error": str(exc)[:300], "refreshed": 0, "skipped": 0, "failed": []}
    return {"refreshed": state.refreshed, "skipped": state.skipped,
            "failed": state.failed[:5], "key_source": state.key_source,
            "storage": str(state.storage)}


def _wechat4_window(previous: dict[str, Any], config: dict[str, Any],
                    initial_days: int = 7, catchup_days: int = 30) -> int:
    """增量窗口下界（unix 秒）。

    平常就是"上次读到哪"，但两种例外要往回补：

    * 没有游标（第一次跑，或刚从 3.x 换到 4.x）→ 回填 `initial_backfill_days` 天；
    * 电脑关了很久 → 最多回补 `catchup_days` 天。这就是"微信关着/电脑没开期间
      更新的推文，开机后一并抓回来"的那条路，同时也避免一上来把几年历史灌进库。
    """
    now = int(time.time())
    initial = max(1, min(365, int(config.get("initial_backfill_days", initial_days) or initial_days)))
    catchup = max(1, min(365, int(config.get("catchup_days", catchup_days) or catchup_days)))
    try:
        value = int(previous.get("last_create_time") or 0)
    except (TypeError, ValueError):
        value = 0
    if not value:
        return now - initial * 86400
    return max(min(value, now - 60), now - catchup * 86400)


def _wechat3_history_candidates(source: dict[str, Any], config: dict[str, Any],
                                previous: dict[str, Any], allowed: set[str],
                                excluded: set[str], names: dict[str, str],
                                enforce_allowlist: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """3.x 的 ``PublicMsg.db``：升级到 4.x 之后不再增长，但升级前的历史一条没丢。

    4.x 只在"用户打开订阅号消息"时才拉一批，所以升级之后 ``biz_message_*``
    可能长时间没有新内容 —— 这个通道就是拿来补上"升级之前那段时间"的。
    它有自己的 ``localId`` 游标，不受 4.x 那套 ``last_create_time`` 影响。
    """
    from . import wechat3_public

    state = previous.get("wechat3") if isinstance(previous.get("wechat3"), dict) else {}
    try:
        snap = wechat3_public.snapshot()
    except Exception as exc:                             # noqa: BLE001
        return [], {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if not snap:
        return [], {"available": False,
                    "reason": "没有 3.x 的 PublicMsg.db，或 wechat_db_key 已失效"}

    max_rows = max(20, min(5000, int(config.get("max_rows_per_poll", 1000))))
    after = int(state.get("local_id") or 0)
    since: int | None = None
    if not after:
        # 第一次接上：按配置的天数回填，而不是从 2023 年一路灌下来。
        days = max(1, min(365, int(config.get("initial_backfill_days", 7) or 7)))
        since = int(time.time()) - days * 86400
    try:
        rows = wechat3_public.articles(since_ts=since, after_local_id=after,
                                       limit=max_rows, snap=snap)
    except Exception as exc:                             # noqa: BLE001
        return [], {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    newest = after
    candidates: list[dict[str, Any]] = []
    for item in rows:
        talker = item["user_name"]
        newest = max(newest, int(item["local_id"]))
        if (enforce_allowlist and talker not in allowed) or talker in excluded:
            continue
        candidates.append({
            "source_id": source["id"], "kind": "article",
            "url": item["url"], "canonical_url": canonical_url(item["url"]),
            # 一次群发多篇共用一个 localId，键里必须带 idx，否则第 2 篇撞键被丢掉
            "external_key": (f"public3:{talker}:{item['local_id']}"
                             + (f"#{item['idx']}" if int(item.get("idx") or 1) > 1 else "")),
            "title": item["title"] or item["url"],
            "author": names.get(talker, item.get("account") or talker),
            "published_at": epoch_iso(item["create_time"]),
            "metadata": {"share_channel": "wechat_public_local", "wechat_version": "3.x-history",
                         "wechat_account_id": talker,
                         "public_message_id": item["local_id"],
                         "public_message_idx": int(item.get("idx") or 1),
                         "description": item.get("digest", ""),
                         "catalog_matched": talker in allowed},
        })
    return candidates, {"available": True, "local_id": newest,
                        "scanned": len(rows), "kept": len(candidates),
                        "backfill_days": (max(1, min(365, int(config.get("initial_backfill_days", 7) or 7)))
                                          if since else None),
                        "source": str(snap["source"]), "cached": snap["cached"]}


def _wechat4_memory_candidates(source: dict[str, Any], config: dict[str, Any],
                               previous: dict[str, Any], names: dict[str, str],
                               allowed: set[str], enforce_allowlist: bool,
                               excluded: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从 Weixin.exe 内存里补 4.x 订阅号盒子的推送。

    4.x 不把订阅号正文写进库，但客户端内存里留着盒子卡片的 JSON：
    ``AppMsgId``(=mid) / ``DateTime`` / ``uniqueId``=``gh_x@mid`` / ``Title``，
    外加一池文章 URL 按 mid 对齐。只读 private 堆，不碰微信任何文件。

    首次运行（seen 为空）会把当前盒子里的条目一次性补齐，然后按 key 增量。
    """
    from . import wechat4_memory

    state = previous.get("memory") if isinstance(previous.get("memory"), dict) else {}
    seen: dict[str, int] = {}
    for key, value in (state.get("seen") or {}).items():
        try:
            seen[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue

    if not config.get("memory_channel", True):
        return [], {"available": False, "reason": "内存通道已关闭（memory_channel=false）",
                    "seen": seen, "kept": 0, "last_scan_at": now_iso()}

    snap = wechat4_memory.scan()
    cursor: dict[str, Any] = {
        "available": snap.ok, "reason": snap.reason, "kept": 0,
        "scanned": len(snap.cards), "urls": len(snap.urls),
        "accounts": len(snap.accounts), "elapsed": round(snap.elapsed, 2),
        "scanned_mb": round(snap.scanned_mb, 1), "last_scan_at": now_iso(),
    }
    if not snap.ok:
        return [], {**cursor, "seen": seen}

    now = int(time.time())
    days = max(1, min(365, int(config.get("initial_backfill_days", 7) or 7)))
    floor = now - days * 86400
    max_new = max(1, min(2000, int(config.get("memory_max_new", 300) or 300)))

    out: list[dict[str, Any]] = []
    skipped_no_url = 0
    for card in snap.cards:
        key = card["key"]
        ts = int(card["ts"])
        if ts < floor or key in seen:
            continue
        gh = card["gh"]
        if (enforce_allowlist and gh not in allowed) or gh in excluded:
            # 名单外的**不记 seen**：以后把它勾进名单还能补回来
            continue
        url = card.get("url") or ""
        if not url:
            skipped_no_url += 1
            continue                  # URL 池还没加载到，下一轮再看
        seen[key] = ts
        out.append({
            "source_id": source["id"], "kind": "article",
            "url": url, "canonical_url": card.get("canonical_url") or canonical_url(url),
            "external_key": f"public4mem:{gh}:{card['key'].split('@', 1)[-1]}",
            "title": card["title"] or url,
            "author": names.get(gh, snap.accounts.get(gh) or gh),
            "published_at": epoch_iso(ts),
            "metadata": {"share_channel": "wechat_memory_box",
                         "wechat_version": "4.x-memory",
                         "wechat_account_id": gh,
                         "public_message_id": card["mid"],
                         "public_message_idx": int(card.get("idx") or 1),
                         "description": "",
                         "account_name": snap.accounts.get(gh, ""),
                         "catalog_matched": gh in allowed},
        })
        if len(out) >= max_new:
            break

    keep = now - 30 * 86400
    seen = {k: v for k, v in seen.items() if v >= keep}
    cursor.update({"kept": len(out), "seen": len(seen),
                   "pending_url": skipped_no_url,
                   "window_days": days})
    return out, {**cursor, "seen": seen}


def _wechat4_public_candidates(module, source: dict[str, Any], config: dict[str, Any],
                               previous: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    directory = module.oa_directory()
    allowed, catalog_matched, unmatched = _public_account_filter(config, directory)
    excluded = {str(value) for value in config.get("exclude_account_ids") or []}
    names = {user: info["name"] for user, info in directory.items()}
    enforce_allowlist = bool(config.get("use_catalog", True) or allowed)

    # 启用名单变了就回填：新勾上的公众号应该马上看到它最近的文章，
    # 而不是从"勾选的这一刻"开始等下一篇。
    effective = sorted(value for value in allowed if value not in excluded) \
        if enforce_allowlist else []
    fingerprint = hashlib.sha1("|".join(effective).encode("utf-8")).hexdigest()[:16]
    days = max(1, min(365, int(config.get("initial_backfill_days", 7) or 7)))
    now = int(time.time())
    if not previous.get("initialized") and config.get("start_at_now", True):
        # 全新接上的来源（从没初始化过）默认从此刻开始记，不把陈年推文灌进来。
        # 注意"从 3.x 换到 4.x"不算全新：游标里 initialized 已经为真，这是升级，
        # 该补的那几天还得补。
        since = now
    elif previous.get("catalog_fingerprint") != fingerprint or not previous.get("last_create_time"):
        since = now - days * 86400
    else:
        since = _wechat4_window(previous, config)

    articles = module.official_articles(since_ts=since)
    candidates: list[dict[str, Any]] = []
    newest = since
    for item in articles:
        talker = item["user_name"]
        newest = max(newest, int(item["create_time"] or 0))
        if (enforce_allowlist and talker not in allowed) or talker in excluded:
            continue
        candidates.append({
            "source_id": source["id"], "kind": "article",
            "url": item["url"], "canonical_url": canonical_url(item["url"]),
            "external_key": f"public4:{talker}:{item['local_id']}",
            "title": item["title"] or item["url"],
            "author": names.get(talker, item["account"]),
            "published_at": epoch_iso(item["create_time"]),
            "metadata": {"share_channel": "wechat_public_local", "wechat_version": "4.x",
                         "wechat_account_id": talker,
                         "public_message_id": item["local_id"],
                         "description": item["digest"],
                         "catalog_matched": talker in catalog_matched},
        })
    # 升级前的历史走 3.x 的 PublicMsg.db：4.x 客户端不落盘公众号正文，
    # 光等 biz_message_* 会一直空着，而那边的库一条没丢。
    history, history_cursor = _wechat3_history_candidates(
        source, config, previous, allowed, excluded, names, enforce_allowlist)
    candidates.extend(history)

    # 4.x 真正的推送流在客户端内存里（盒子卡片的 JSON），库是空的。这条是
    # 「不用点开任何推文就能自动拿到全部更新」的唯一本地路径。
    memory, memory_cursor = _wechat4_memory_candidates(
        source, config, previous, names, allowed, enforce_allowlist, excluded)
    candidates.extend(memory)

    cursor = {"initialized": True, "wechat_version": "4.x",
              "last_create_time": max(newest, int(previous.get("last_create_time") or 0)),
              "catalog_fingerprint": fingerprint,
              "tracked_accounts": len(effective),
              "last_read_at": now_iso(), "matched_links": len(candidates),
              "catalog_matched_accounts": len(catalog_matched),
              "catalog_unmatched_names": unmatched, "warnings": [],
              "wechat3": history_cursor, "memory": memory_cursor}
    if not effective:
        cursor["warnings"].append("还没有勾选要追踪的公众号，本批不采集任何推文")
    if not history_cursor.get("available"):
        cursor["warnings"].append(f"3.x 历史库不可用：{history_cursor.get('reason', '未知原因')}")
    if memory_cursor.get("reason"):
        cursor["warnings"].append(f"内存通道：{memory_cursor['reason']}")
    return candidates, cursor


def _wechat4_self_candidates(module, source: dict[str, Any], config: dict[str, Any],
                             previous: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    since = _wechat4_window(previous, config, initial_days=3)
    items = module.self_links(since_ts=since)
    candidates: list[dict[str, Any]] = []
    newest = since
    for item in items:
        newest = max(newest, int(item["create_time"] or 0))
        candidates.append({
            "source_id": source["id"], "kind": "article",
            "url": item["url"], "canonical_url": canonical_url(item["url"]),
            "external_key": f"self4:{item['user_name']}:{item['local_id']}",
            "title": item["title"] or item["url"], "author": "当前微信自己转发的",
            "published_at": epoch_iso(item["create_time"]),
            "metadata": {"share_channel": "wechat_self_chat", "wechat_version": "4.x",
                         "message_id": item["local_id"],
                         "description": item["digest"]},
        })
    cursor = {"initialized": True, "wechat_version": "4.x", "last_create_time": newest,
              "last_read_at": now_iso(), "matched_links": len(candidates), "warnings": []}
    return candidates, cursor


async def poll_wechat_self(source: dict[str, Any], scope: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = json_loads(source.get("config_json"), {}) or {}
    previous = json_loads(source.get("cursor_json"), {}) or {}
    if local_channel() == "4.x":
        module = _wechat4()
        if module is not None:
            # 先刷新解密缓存再读：否则读到的还是上一次同步的旧快照，
            # "微信关着的时候进来的消息"要等下一轮才看得到。
            sync = _wechat4_sync(module)
            candidates, cursor = _wechat4_self_candidates(module, source, config, previous)
            cursor["sync"] = sync
            return _apply_history_policy(candidates, cursor, config, source["id"])
    candidates, cursor = sync_self_chat(previous,
                                        config.get("target_wxid", ""), config.get("target_name", ""))
    return _apply_history_policy(candidates, cursor, config, source["id"])


def _apply_history_policy(candidates: list[dict[str, Any]], cursor: dict[str, Any],
                          config: dict[str, Any],
                          source_id: Any = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """切换监测时间点之前的历史链接按 history_mode 处理（3.x / 4.x 两条通道共用）。"""
    cutoff = parse_time(config.get("auto_process_after"))
    history_mode = config.get("history_mode", "skip")
    kept: list[dict[str, Any]] = []
    skipped_history = 0
    for candidate in candidates:
        candidate["source_id"] = source_id
        if config.get("target_name"):
            candidate["author"] = config["target_name"]
            candidate.setdefault("metadata", {})["target_name"] = config["target_name"]
        published = parse_time(candidate.get("published_at"))
        if cutoff and (not published or published <= cutoff):
            # Everything before the switch-over is old history. Indexing it filled the
            # library with years of unrelated chat links, so it is skipped by default.
            if history_mode == "skip":
                skipped_history += 1
                continue
            candidate["defer_processing"] = True
            candidate.setdefault("metadata", {})["processing_policy"] = "history_index_only"
        kept.append(candidate)
    if skipped_history:
        cursor["skipped_history"] = skipped_history
        cursor.setdefault("warnings", []).append(f"跳过 {skipped_history} 条切换监测前的历史链接")
    return kept, cursor


def _official_account_directory(root: Path, key: str) -> dict[str, dict[str, Any]]:
    conn = _open_live_readonly(root / "Msg" / "MicroMsg.db", key)
    try:
        rows = conn.execute("""SELECT UserName,NickName,Remark FROM Contact
            WHERE substr(UserName,1,3)='gh_'""").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for user, nickname, remark in rows:
            aliases = {_account_name_key(str(value)) for value in (nickname, remark) if str(value or "").strip()}
            result[str(user)] = {"name": str(remark or nickname or user), "aliases": aliases}
        return result
    finally:
        conn.close()


def _official_account_names(root: Path, key: str) -> dict[str, str]:
    return {user: info["name"] for user, info in _official_account_directory(root, key).items()}


def _account_name_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _public_account_filter(config: dict[str, Any], directory: dict[str, dict[str, Any]]) -> tuple[set[str], set[str], int]:
    """Join the local connector to the same enabled catalog used by the web UI."""
    allowed = {str(value) for value in config.get("account_ids") or []}
    catalog_matched = set(allowed)
    unmatched = 0
    if config.get("use_catalog", True):
        from ..db import Database
        accounts = Database().query("SELECT name,account_id FROM wechat_accounts WHERE enabled=1 AND fit!='EXCLUDE'")
        name_index: dict[str, list[str]] = {}
        # 名字对不上 gh 时退一步用 OCR 折叠键：公众号库里有一批名字是 OCR 出来的，
        # 「Al-Protein Design」（小写 L）其实是「AI-Protein Design」。不退这一步，
        # 这些号永远进不了名单，表现就是「明明关注了却一条都收不到」。
        ocr_index: dict[str, list[str]] = {}
        for user, info in directory.items():
            for alias in info["aliases"]:
                name_index.setdefault(alias, []).append(user)
                ocr_index.setdefault(name_match_key(alias), []).append(user)
        for account in accounts:
            matches: set[str] = set()
            if account.get("account_id"):
                matches.add(str(account["account_id"]))
            matches.update(name_index.get(_account_name_key(account["name"]), []))
            if not matches:
                hits = set(ocr_index.get(name_match_key(account["name"]), []))
                if len(hits) == 1:               # 折叠会带来误撞，必须唯一命中才认
                    matches.update(hits)
            if matches:
                allowed.update(matches)
                catalog_matched.update(matches)
            else:
                unmatched += 1
    return allowed, catalog_matched, unmatched


def _database_generation(source: Path) -> str:
    """The random SQLCipher salt changes when a database is deleted and recreated."""
    with source.open("rb") as handle:
        salt = handle.read(16)
    if len(salt) != 16:
        raise RuntimeError("公众号数据库缺少有效文件头")
    return hashlib.sha256(salt).hexdigest()


async def poll_wechat_public_local(source: dict[str, Any], scope: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Incrementally read selected official accounts from the current PC WeChat."""
    config = json_loads(source.get("config_json"), {}) or {}
    previous = json_loads(source.get("cursor_json"), {}) or {}
    if local_channel() == "4.x":
        module = _wechat4()
        if module is not None:
            # 顺序不能反：refresh 把新写进库里的消息（含 WAL 里还没落盘的）解出来，
            # 再读才看得到"微信关着那段时间"的推文。
            sync = _wechat4_sync(module)
            try:
                candidates, cursor = _wechat4_public_candidates(module, source, config, previous)
            except Exception as exc:                # noqa: BLE001
                raise RuntimeError(f"微信 4.x 通道读取公众号失败：{exc}") from exc
            cursor["sync"] = sync
            return candidates, cursor
    _, root, key = _account_and_key()
    database = root / "Msg" / "PublicMsg.db"
    if not database.is_file():
        raise RuntimeError("当前微信没有 PublicMsg.db；请登录并打开一次“订阅号消息”后重试；已有雷达资料不会受影响")
    generation = _database_generation(database)
    warnings: list[str] = []
    if previous.get("initialized") and previous.get("database_generation") != generation:
        # A moved/copied database retains its salt. A newly created one does not.
        previous = {}
        warnings.append("检测到公众号数据库已重建，已安全重置增量游标；雷达已有条目不受影响")
    conn = _open_live_readonly(database, key)
    candidates: list[dict[str, Any]] = []
    try:
        current_max = int(conn.execute("SELECT COALESCE(max(localId),0) FROM PublicMsg").fetchone()[0])
        if not previous.get("initialized"):
            if config.get("start_at_now", True):
                directory = _official_account_directory(root, key)
                _, catalog_matched, unmatched = _public_account_filter(config, directory)
                return [], {"initialized": True, "last_local_id": current_max, "database_generation": generation,
                            "catalog_matched_accounts": len(catalog_matched), "catalog_unmatched_names": unmatched,
                            "last_read_at": now_iso(), "matched_links": 0, "backfill_pending": False, "warnings": warnings}
            days = max(1, min(365, int(config.get("initial_backfill_days", 1))))
            import datetime
            previous["backfill_after"] = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).timestamp())
            previous["last_local_id"] = 0
        last_local_id = int(previous.get("last_local_id") or 0)
        if last_local_id > current_max:
            warnings.append("检测到公众号消息编号回退，可能发生了恢复或重建；已回到当前末尾，避免漏掉后续新文章")
            last_local_id = current_max
            previous.pop("backfill_after", None)
        max_rows = max(20, min(5000, int(config.get("max_rows_per_poll", 1000))))
        conditions = ["Type=49", "SubType=5", "localId>?"]
        params: list[Any] = [last_local_id]
        if previous.get("backfill_after") is not None:
            conditions.append("CreateTime>=?")
            params.append(int(previous["backfill_after"]))
        params.append(max_rows + 1)
        rows = conn.execute(f"""SELECT localId,CreateTime,StrTalker,StrContent,CompressContent
            FROM PublicMsg WHERE {' AND '.join(conditions)} ORDER BY localId LIMIT ?""", params).fetchall()
        has_more = len(rows) > max_rows
        rows = rows[:max_rows]
        directory = _official_account_directory(root, key) if rows else {}
        if rows:
            allowed, catalog_matched, unmatched = _public_account_filter(config, directory)
            matched_account_count = len(catalog_matched)
        else:
            allowed = {str(value) for value in config.get("account_ids") or []}
            catalog_matched = set(allowed)
            matched_account_count = int(previous.get("catalog_matched_accounts", 0))
            unmatched = int(previous.get("catalog_unmatched_names", 0))
        names = {user: info["name"] for user, info in directory.items()}
        excluded = {str(value) for value in config.get("exclude_account_ids") or []}
        enforce_allowlist = bool(config.get("use_catalog", True) or allowed)
        seen: set[str] = set()
        unparsed = 0
        for local_id, created, talker, text, compressed in rows:
            last_local_id = int(local_id)
            if (enforce_allowlist and talker not in allowed) or talker in excluded:
                continue
            title, description, url, _ = _card(compressed, str(text or ""))
            host = (urlsplit(url).hostname or "").lower() if url else ""
            if host != "mp.weixin.qq.com" and not host.endswith(".mp.weixin.qq.com"):
                unparsed += 1
                continue
            try:
                canonical = canonical_url(url)
            except ValueError:
                unparsed += 1
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            candidates.append({
                "source_id": source["id"], "kind": "article", "url": url, "canonical_url": canonical,
                "external_key": f"public:{local_id}", "title": title or url,
                "author": names.get(talker, talker), "published_at": epoch_iso(created),
                "metadata": {"share_channel": "wechat_public_local", "wechat_account_id": talker,
                             "public_message_id": local_id, "description": description,
                             "catalog_matched": talker in catalog_matched},
            })
        if unparsed:
            warnings.append(f"本批有 {unparsed} 条非文章或无法解析卡片，已跳过并推进游标")
        cursor = {"initialized": True, "last_local_id": last_local_id, "database_generation": generation,
                  "last_read_at": now_iso(), "matched_links": len(candidates), "rows_read": len(rows),
                  "unparsed_cards": unparsed, "catalog_matched_accounts": matched_account_count,
                  "catalog_unmatched_names": unmatched, "backfill_pending": has_more, "warnings": warnings}
        if has_more and previous.get("backfill_after") is not None:
            cursor["backfill_after"] = previous["backfill_after"]
        return candidates, cursor
    finally:
        conn.close()
