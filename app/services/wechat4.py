"""微信 4.x（Weixin.exe）只读适配：取密钥、认数据库、列消息。

和 3.x 的差别（都是 4.0 之后换的实现）：

* 数据目录从 ``Documents\\WeChat Files\\<wxid>`` 换成
  ``Documents\\xwechat_files\\<wxid>\\db_storage``，库按用途拆成
  ``contact/contact.db``、``message/message_0..N.db``、``session/session.db`` …
* 加密从 SQLCipher 3 换成 SQLCipher 4：AES-256-CBC + HMAC-SHA512，
  PBKDF2-HMAC-SHA512 256000 轮，页大小 4096，每页预留 80 字节（IV 16 + HMAC 64）。
* 密钥不再以 32 字节裸数据散在内存里，而是 WCDB 的
  ``com.Tencent.WCDB.Config.Cipher`` 配置对象里一串 ASCII ``x'<64 hex>'``。
* 每个库有自己的 salt 和密钥，所以必须逐库试，不能一把钥匙开所有门。
* 消息正文常常是 zstd 压过的，读出来要先解压。

这个模块只做只读扫描：不注入、不改写、不发消息。没有 4.x 环境时它不会
被调用，调用失败也只返回结构化错误，不会影响 3.x 的既有链路。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sqlite3
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterable

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}  # READONLY / READWRITE / WRITECOPY / EXECUTE_*
KEY_PATTERN = re.compile(rb"x'([0-9a-fA-F]{64})'")


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


# --------------------------------------------------------------------------- #
# 目录与进程
# --------------------------------------------------------------------------- #
def weixin_process() -> Any | None:
    try:
        import psutil
    except ImportError:
        return None
    for process in psutil.process_iter(["name", "exe", "pid"]):
        if (process.info.get("name") or "").lower() == "weixin.exe":
            return process
    return None


def candidate_roots() -> list[Path]:
    """只找已知位置，不扫盘。"""
    roots: list[Path] = []
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin") as key:
                value, _ = winreg.QueryValueEx(key, "FileSavePath")
                if value:
                    roots.append(Path(os.path.expandvars(str(value))))
        except OSError:
            pass
    home = Path.home()
    roots += [home / "Documents" / "xwechat_files", home / "xwechat_files"]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        marker = str(root).casefold()
        if root.is_dir() and marker not in seen:
            seen.add(marker)
            unique.append(root)
    return unique


def db_roots() -> list[Path]:
    """返回每账号的 db_storage 目录。"""
    found: list[Path] = []
    for root in candidate_roots():
        for child in root.iterdir() if root.is_dir() else []:
            storage = child / "db_storage"
            if storage.is_dir():
                found.append(storage)
    return found


def databases(storage: Path) -> list[Path]:
    return sorted(path for path in storage.rglob("*.db") if path.is_file())


def quick_status() -> dict[str, Any]:
    """不碰内存的版本，给设置页『检测』用。"""
    process = weixin_process()
    storages = db_roots()
    return {
        "running": process is not None,
        "pid": getattr(process, "pid", 0) if process else 0,
        "storages": [str(path) for path in storages],
        "databases": sum(len(databases(path)) for path in storages),
        "template": (storages[0] / "message").is_dir() if storages else False,
    }


# --------------------------------------------------------------------------- #
# 内存里找密钥
# --------------------------------------------------------------------------- #
def _readable_regions(handle: int) -> Iterable[tuple[int, int]]:
    kernel32 = ctypes.windll.kernel32
    address = 0
    info = _MemoryBasicInformation()
    while kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info),
                                  ctypes.sizeof(info)):
        base = int(info.BaseAddress or 0)
        size = int(info.RegionSize or 0)
        if size <= 0:
            break
        if (info.State == MEM_COMMIT and info.Protect in READABLE
                and not (info.Protect & PAGE_NOACCESS) and not (info.Protect & PAGE_GUARD)):
            yield base, size
        address = base + size


def scan_keys(pid: int, max_seconds: float = 25.0) -> dict[str, Any]:
    """在 Weixin.exe 的内存里找 x'<64 hex>'，返回去重后的候选密钥。"""
    if os.name != "nt":
        return {"keys": [], "error": "只有 Windows 支持"}
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        hint = "（访问被拒绝，试试以管理员身份运行）" if error == 5 else ""
        return {"keys": [], "error": f"OpenProcess 失败，Windows 错误 {error}{hint}"}

    keys: list[str] = []
    seen: set[str] = set()
    scanned = 0
    started = time.monotonic()
    try:
        for base, size in _readable_regions(handle):
            if time.monotonic() - started > max_seconds:
                break
            offset = 0
            chunk = 8 * 1024 * 1024
            while offset < size:
                length = min(chunk, size - offset)
                buffer = ctypes.create_string_buffer(length)
                read = ctypes.c_size_t(0)
                ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base + offset), buffer,
                                                length, ctypes.byref(read))
                if ok:
                    data = buffer.raw[: read.value]
                    scanned += len(data)
                    for match in KEY_PATTERN.finditer(data):
                        key = match.group(1).decode("ascii").lower()
                        if key not in seen:
                            seen.add(key)
                            keys.append(key)
                offset += length
    finally:
        kernel32.CloseHandle(handle)
    return {"keys": keys, "scanned_mb": round(scanned / 1024 / 1024, 1),
            "seconds": round(time.monotonic() - started, 1)}


# --------------------------------------------------------------------------- #
# 用密钥打开库
# --------------------------------------------------------------------------- #
def _sqlcipher_open(path: Path, key: str):
    from sqlcipher3 import dbapi2 as sqlcipher
    conn = sqlcipher.connect(str(path))
    try:
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
        conn.execute("PRAGMA cipher_compatibility = 4")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("PRAGMA kdf_iter = 256000")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
        conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except Exception:
        conn.close()
        raise


def verify_key(path: Path, key: str) -> bool:
    try:
        conn = _sqlcipher_open(path, key)
    except Exception:
        return False
    conn.close()
    return True


def unlock(storage: Path, keys: list[str], budget_seconds: float = 90.0) -> dict[str, Any]:
    """逐库试密钥；每个库试到能打开为止，成功就记住。"""
    started = time.monotonic()
    opened: dict[str, str] = {}
    failed: list[str] = []
    for database in databases(storage):
        if time.monotonic() - started > budget_seconds:
            failed.append(f"{database.name}（超出时间预算）")
            continue
        for key in keys:
            if verify_key(database, key):
                opened[str(database.relative_to(storage))] = key
                break
        else:
            failed.append(str(database.relative_to(storage)))
    return {"opened": opened, "failed": failed,
            "seconds": round(time.monotonic() - started, 1)}


# --------------------------------------------------------------------------- #
# 读消息
# --------------------------------------------------------------------------- #
def _decompress(blob: bytes) -> bytes:
    """4.x 的正文常常是 zstd；没有 zstandard 就原样返回。"""
    if not blob or blob[:4] != b"\x28\xb5\x2f\xfd":
        return blob
    try:
        import zstandard
    except ImportError:
        return b""
    try:
        return zstandard.ZstdDecompressor().decompress(blob, max_output_size=8 << 20)
    except Exception:
        return b""


URL_PATTERN = re.compile(rb"https?://mp\.weixin\.qq\.com/s[^\"'<>\\\s]+")


def message_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return [row[0] for row in rows if str(row[0]).lower().startswith("msg_")]


def recent_message_links(storage: Path, key_by_db: dict[str, str], limit: int = 200) -> dict[str, Any]:
    """在 message_*.db 里找公众号文章链接。字段名各版本略有差异，所以按列名猜。"""
    links: list[dict[str, str]] = []
    errors: list[str] = []
    for relative, key in key_by_db.items():
        if not relative.startswith("message"):
            continue
        path = storage / relative
        try:
            conn = _sqlcipher_open(path, key)
        except Exception as exc:
            errors.append(f"{relative}: {exc}")
            continue
        try:
            for table in message_tables(conn)[:40]:
                try:
                    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
                except sqlite3.Error:
                    continue
                blob_column = next((c for c in columns if c.lower() in
                                    ("message_content", "content", "msgcontent", "compress_content")), None)
                if not blob_column:
                    continue
                try:
                    rows = conn.execute(
                        f'SELECT "{blob_column}" FROM "{table}" WHERE "{blob_column}" IS NOT NULL '
                        f"ORDER BY rowid DESC LIMIT {int(limit)}").fetchall()
                except sqlite3.Error as exc:
                    errors.append(f"{relative}/{table}: {exc}")
                    continue
                for (blob,) in rows:
                    if not isinstance(blob, (bytes, bytearray)):
                        continue
                    data = _decompress(bytes(blob))
                    if not data:
                        continue
                    for match in URL_PATTERN.finditer(data):
                        url = match.group(0).decode("utf-8", "replace").rstrip("&")
                        if url not in [item["url"] for item in links]:
                            links.append({"url": url, "source": f"{relative}/{table}"})
        finally:
            conn.close()
    return {"links": links, "errors": errors}


def probe(max_seconds: float = 25.0) -> dict[str, Any]:
    """一次完整的 4.x 探测：找进程 → 找目录 → 扫密钥 → 试开库 → 统计。"""
    process = weixin_process()
    if process is None:
        return {"ok": False, "reason": "Weixin.exe 没在运行；4.x 的密钥只在运行时存在，请先登录微信"}
    storages = db_roots()
    if not storages:
        return {"ok": False, "reason": "没找到 xwechat_files 数据目录；确认装的是微信 4.x 并登录过"}
    scan = scan_keys(process.pid, max_seconds=max_seconds)
    if not scan["keys"]:
        return {"ok": False, "reason": scan.get("error") or "内存里没找到 x'…' 形式的密钥",
                "scanned_mb": scan.get("scanned_mb")}
    storage = storages[0]
    unlocked = unlock(storage, scan["keys"])
    return {
        "ok": bool(unlocked["opened"]),
        "pid": process.pid,
        "storage": str(storage),
        "keys_found": len(scan["keys"]),
        "scanned_mb": scan.get("scanned_mb"),
        "opened": list(unlocked["opened"].keys()),
        "failed": unlocked["failed"],
        "seconds": unlocked["seconds"],
    }
