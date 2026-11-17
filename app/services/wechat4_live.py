"""微信 4.x（Weixin.exe）实时只读数据源：取密钥 → 解密（含 WAL 合并）→ 出条目。

2026-11 在 WeChat 4.2.0.53 / Windows 11 上实测通过的实现，替换掉
``wechat4.py`` 里那套没有验证过的假设（4.2 已经不再把 ``x'<key>'`` 明文放在
内存里，老办法扫不到）。

原理：

1. **密钥**：4.2 把每库的密钥放在 WCDB 的 ``com.Tencent.WCDB.Config.Cipher``
   配置对象里，并用固定掩码异或过。先在内存里找这个字符串的地址，再找
   ``[ptr][len]`` 那对引用，回溯到配置对象，取出 ``[ptr][len]`` 数据块，
   异或还原成 ``x'<64hex 加密密钥><32hex salt>'``。这 64 位十六进制就是最终
   AES-256 密钥（每库一把），不需要再跑 256000 轮 KDF。
2. **加密**：SQLCipher 4，页 4096，AES-256-CBC，每页末尾保留 80 字节
   = 16 字节 IV + 64 字节 HMAC-SHA512；第 1 页前 16 字节是 salt。
3. **时效**：微信是 WAL 模式，主库文件会滞后几十秒到几分钟。这里把 ``-wal``
   里已提交的帧也解出来按页合并，读到的是"此刻"的状态。帧的盐和 WAL 头不一致
   就停（那是上一代 checkpoint 的残留帧，混进来会把库拼坏）。

全程只读：只用 OpenProcess + ReadProcessMemory 读密钥，只读打开微信的库文件，
解密结果写在自己的缓存目录，绝不写微信任何文件。
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..paths import DATA_DIR

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
KEY_SZ = 32
SQLITE_HDR = b"SQLite format 3\x00"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
MAX_USER_ADDR = 0x0000_8000_0000_0000
CONFIG_BLOB_MAX = 1024

CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
# 4.2.0.53 实测掩码（与 wcdb-key-tool 公开实现一致）
CIPHER_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
ZSTD_FLAGS = {1, 4}

ARTICLE_URL = re.compile(
    r"<url>(?:<!\[CDATA\[)?(https?://mp\.weixin\.qq\.com/s[^<\]]+?)(?:\]\]>)?</url>")
TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
DES_RE = re.compile(r"<des>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</des>", re.S)
NICK_RE = re.compile(r"<nickname>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</nickname>", re.S)
ANY_URL = re.compile(r"https?://[^\s\"'<>\\]+")

MSG_TYPE_NAMES = {1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情",
                  48: "位置", 49: "公众号", 50: "通话", 10000: "系统", 10002: "系统"}


# --------------------------------------------------------------------------- #
# 1. 从运行中的微信进程取密钥
# --------------------------------------------------------------------------- #
class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("__a1", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("__a2", wintypes.DWORD)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.ReadProcessMemory.restype = wintypes.BOOL
_k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.VirtualQueryEx.restype = ctypes.c_size_t
_k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                ctypes.POINTER(_MBI), ctypes.c_size_t]


def weixin_pids() -> list[int]:
    """按内存占用从大到小列出 Weixin.exe 的 PID。

    走 psutil 而不是 tasklist：这台机器上 tasklist 被安全策略拦掉，
    subprocess 拿回的是 None，整个读取器会在第一步就摔。
    """
    try:
        import psutil
    except Exception:
        return []
    found: list[tuple[int, int]] = []
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower() != "weixin.exe":
                continue
            found.append((proc.pid, proc.memory_info().rss))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    found.sort(key=lambda item: item[1], reverse=True)
    return [pid for pid, _rss in found]


def _regions(handle: int) -> Iterator[tuple[int, int]]:
    address, info = 0, _MBI()
    while _k32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info),
                             ctypes.sizeof(info)):
        base, size = int(info.BaseAddress or 0), int(info.RegionSize or 0)
        if size <= 0:
            break
        if (info.State == MEM_COMMIT and info.Protect in READABLE
                and not (info.Protect & PAGE_NOACCESS) and not (info.Protect & PAGE_GUARD)):
            yield base, size
        address = base + size


def _read(handle: int, address: int, size: int) -> bytes:
    if size <= 0 or address <= 0:
        return b""
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if _k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size,
                              ctypes.byref(got)) and got.value:
        return buffer.raw[:got.value]
    return b""


def _u64(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def _xor_repeat(data: bytes, mask: bytes) -> bytes:
    return bytes(value ^ mask[index % len(mask)] for index, value in enumerate(data))


def _probable_key(data: bytes) -> bool:
    return (len(data) == KEY_SZ and len(set(data)) >= 15
            and data not in {b"\x00" * KEY_SZ, b"\xff" * KEY_SZ})


@dataclass
class KeyHit:
    key_hex: str
    salt_hex: str | None


def _candidates_from_blob(blob: bytes) -> list[KeyHit]:
    if not blob or len(blob) > CONFIG_BLOB_MAX:
        return []
    decoded = _xor_repeat(blob, CIPHER_XOR_MASK)
    hits: list[KeyHit] = []
    seen: set[tuple[str, str | None]] = set()
    for match in LITERAL_RE.finditer(decoded):
        run = match.group(1).decode("ascii", "replace").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            key_hex = run[start:start + 64]
            try:
                if not _probable_key(bytes.fromhex(key_hex)):
                    continue
            except ValueError:
                continue
            salt_hex = run[start + 64:start + 96] if start + 96 <= len(run) else None
            item = (key_hex, salt_hex)
            if item not in seen:
                seen.add(item)
                hits.append(KeyHit(key_hex, salt_hex))
    return hits


def _verify(key: bytes, page1: bytes) -> bool:
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, KEY_SZ)
    mac = hmac.new(mac_key, page1[SALT_SZ:PAGE_SZ - RESERVE_SZ + IV_SZ], hashlib.sha512)
    mac.update(struct.pack("<I", 1))
    return mac.digest() == page1[PAGE_SZ - HMAC_SZ:PAGE_SZ]


def salt_of(path: Path) -> str:
    with open(path, "rb") as handle:
        return handle.read(SALT_SZ).hex()


def scan_keys(pids: list[int] | None = None, budget: float = 90.0) -> list[KeyHit]:
    """在 Weixin.exe 内存里把 Config.Cipher 对象里的密钥全捞出来。"""
    if os.name != "nt":
        return []
    started = time.monotonic()
    found: list[KeyHit] = []
    seen: set[tuple[str, str | None]] = set()
    for pid in (pids or weixin_pids()):
        handle = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            continue
        try:
            # 第一步：定位字符串出现的地址
            needle: set[int] = set()
            for base, size in _regions(handle):
                if time.monotonic() - started > budget:
                    break
                offset = 0
                while offset < size:
                    chunk_len = min(4 << 20, size - offset)
                    data = _read(handle, base + offset, chunk_len)
                    if data:
                        pos = data.find(CIPHER_NAME)
                        while pos >= 0:
                            needle.add(base + offset + pos)
                            pos = data.find(CIPHER_NAME, pos + 1)
                    offset += chunk_len
            if not needle:
                continue
            pairs = [struct.pack("<Q", addr) + struct.pack("<Q", len(CIPHER_NAME))
                     for addr in needle]
            # 第二步：找 [ptr][len] 引用 → 回溯节点 → 取配置对象里的数据块
            for base, size in _regions(handle):
                if time.monotonic() - started > budget:
                    break
                offset = 0
                while offset < size:
                    chunk_len = min(4 << 20, size - offset)
                    data = _read(handle, base + offset, chunk_len)
                    if not data:
                        offset += chunk_len
                        continue
                    for pattern in pairs:
                        pos = data.find(pattern)
                        while pos >= 0:
                            node = _read(handle, base + offset + pos - 0x10, 0x50)
                            if (len(node) >= 0x30
                                    and _u64(node, 0x10) in needle
                                    and _u64(node, 0x18) == len(CIPHER_NAME)):
                                config_ptr = _u64(node, 0x28)
                                if 0x10000 <= config_ptr < MAX_USER_ADDR:
                                    obj = _read(handle, config_ptr + 0x88, 0x28)
                                    if len(obj) >= 0x18:
                                        data_ptr, data_len = _u64(obj, 0x8), _u64(obj, 0x10)
                                        if (0 < data_len <= CONFIG_BLOB_MAX
                                                and 0x10000 <= data_ptr < MAX_USER_ADDR):
                                            blob = _read(handle, data_ptr, int(data_len))
                                            for hit in _candidates_from_blob(blob):
                                                key = (hit.key_hex, hit.salt_hex)
                                                if key not in seen:
                                                    seen.add(key)
                                                    found.append(hit)
                            pos = data.find(pattern, pos + 1)
                    offset += chunk_len
        finally:
            _k32.CloseHandle(handle)
    return found


# --------------------------------------------------------------------------- #
# 2. 解密（含 WAL 合并）
# --------------------------------------------------------------------------- #
def _aes():
    from Crypto.Cipher import AES  # pycryptodome
    return AES


def _decrypt_page(key: bytes, page: bytes, pgno: int) -> bytes:
    AES = _aes()
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    if pgno == 1:
        plain = cipher.decrypt(page[SALT_SZ: PAGE_SZ - RESERVE_SZ])
        return SQLITE_HDR + plain + b"\x00" * RESERVE_SZ
    plain = cipher.decrypt(page[: PAGE_SZ - RESERVE_SZ])
    return plain + b"\x00" * RESERVE_SZ


def _mac_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, KEY_SZ)


def _page_hmac_ok(mac_key: bytes, page: bytes, pgno: int) -> bool:
    """SQLCipher 的每页 HMAC：覆盖密文（+第 1 页跳过 salt）+ 页码。"""
    start = SALT_SZ if pgno == 1 else 0
    digest = hmac.new(mac_key, page[start:PAGE_SZ - RESERVE_SZ + IV_SZ], hashlib.sha512)
    digest.update(struct.pack("<I", pgno))
    return digest.digest() == page[PAGE_SZ - HMAC_SZ:PAGE_SZ]


def wal_pages(wal_path: Path, key: bytes, mac_key: bytes) -> tuple[dict[int, bytes], int]:
    """解出 WAL 里已提交且 HMAC 校验通过的页；返回 (页字典, 提交后的页数)。

    WAL 可能正被微信写入，也可能跨了一次 checkpoint。所以逐帧验 HMAC：
    一旦某帧验不过，后面的帧一律不要（说明读到的是半个事务或旧盐）。
    """
    if not wal_path.is_file():
        return {}, 0
    data = wal_path.read_bytes()
    if len(data) < 32:
        return {}, 0
    magic = struct.unpack(">I", data[:4])[0]
    if magic not in (0x377F0682, 0x377F0683):
        return {}, 0
    page_size = struct.unpack(">I", data[8:12])[0]
    if page_size != PAGE_SZ:
        return {}, 0
    wal_salt1, wal_salt2 = struct.unpack(">II", data[16:24])
    stride = 24 + page_size
    frames = (len(data) - 32) // stride
    pages: dict[int, bytes] = {}
    committed: dict[int, bytes] = {}
    commit_size = 0
    for index in range(frames):
        offset = 32 + index * stride
        pgno, dbsize, salt1, salt2 = struct.unpack(">IIII", data[offset:offset + 16])
        # 帧的盐必须和 WAL 头一致；不一致说明是上一代 checkpoint 的残留帧，
        # 内容虽然是合法密文、HMAC 也过得去，但混进来就会把库拼坏。
        if salt1 != wal_salt1 or salt2 != wal_salt2:
            break
        page = data[offset + 24: offset + 24 + page_size]
        if pgno == 0 or len(page) < page_size:
            break
        if not _page_hmac_ok(mac_key, page, pgno):
            break
        try:
            pages[pgno] = _decrypt_page(key, page, pgno)
        except Exception:                            # noqa: BLE001
            break
        if dbsize:
            committed = dict(pages)
            commit_size = dbsize
    return committed, commit_size


def decrypt_database(db_path: Path, out_path: Path, key_hex: str,
                     merge_wal: bool = True) -> dict[str, Any]:
    """解一个库；有 WAL 就把已提交的页合并进去。"""
    key = bytes.fromhex(key_hex)
    raw = db_path.read_bytes()
    if len(raw) < PAGE_SZ:
        return {"ok": False, "reason": "文件太小"}
    page1 = raw[:PAGE_SZ]
    if not _verify(key, page1):
        return {"ok": False, "reason": "HMAC 校验失败（密钥不匹配）"}
    mac_key = _mac_key(key, page1[:SALT_SZ])
    main_pages = len(raw) // PAGE_SZ
    overrides: dict[int, bytes] = {}
    wal_size = 0
    if merge_wal:
        overrides, wal_size = wal_pages(db_path.with_name(db_path.name + "-wal"),
                                        key, mac_key)
    total = wal_size or main_pages
    merged_p1 = overrides.get(1) or _decrypt_page(key, page1, 1)
    # 页 1 的 SQLite 头里记着真实页数；只有在手头真有这么多页时才采信
    if len(merged_p1) >= 32:
        header_pages = struct.unpack(">I", merged_p1[28:32])[0]
        available = max(main_pages, max(overrides) if overrides else 0)
        if 0 < header_pages <= available:
            total = header_pages
    total = max(total, 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as handle:
        for pgno in range(1, total + 1):
            page = overrides.get(pgno)
            if page is None:
                start = (pgno - 1) * PAGE_SZ
                if start >= len(raw):
                    page = b"\x00" * PAGE_SZ
                else:
                    chunk = raw[start:start + PAGE_SZ].ljust(PAGE_SZ, b"\x00")
                    page = _decrypt_page(key, chunk, pgno)
            handle.write(page)
    return {"ok": True, "pages": total, "wal_pages": len(overrides)}


# --------------------------------------------------------------------------- #
# 3. 数据目录发现与整体刷新
# --------------------------------------------------------------------------- #
def _ini_roots() -> list[Path]:
    """4.x 把数据根目录写在 %APPDATA%\\Tencent\\xwechat\\config\\*.ini 里（纯文本）。"""
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return roots
    config = Path(appdata) / "Tencent" / "xwechat" / "config"
    if not config.is_dir():
        return roots
    for ini in config.glob("*.ini"):
        try:
            text = ini.read_text("utf-8", "ignore").strip()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip().strip('"')
            if line and Path(line).is_absolute():
                roots.append(Path(line))
    return roots


def _registry_roots() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        import winreg
        for hive, sub in ((winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
                          (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat")):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    for name in ("FileSavePath", "InstallPath", "FileSavePathV2"):
                        try:
                            value, _ = winreg.QueryValueEx(key, name)
                        except OSError:
                            continue
                        if value:
                            roots.append(Path(os.path.expandvars(str(value))))
            except OSError:
                continue
    roots += _ini_roots()
    home = Path.home()
    roots += [home / "Documents" / "xwechat_files", home / "xwechat_files"]
    return roots


def db_storages() -> list[Path]:
    """找到 <root>/<account>/db_storage。"""
    found: list[Path] = []
    seen: set[str] = set()
    for root in _registry_roots():
        if not root.is_dir():
            continue
        for base in (root, root / "xwechat_files"):
            if not base.is_dir():
                continue
            for child in base.iterdir():
                storage = child / "db_storage"
                key = str(storage).casefold()
                if storage.is_dir() and key not in seen:
                    seen.add(key)
                    found.append(storage)
    return found


@dataclass
class SyncState:
    storage: Path
    cache: Path
    keys: dict[str, str]           # salt -> key_hex
    key_source: str = ""
    refreshed: int = 0
    skipped: int = 0
    failed: list[str] = None        # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failed is None:
            self.failed = []


DEFAULT_CACHE = DATA_DIR / "wechat4"


def load_key_cache(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def refresh(storage: Path | None = None, cache: Path | None = None,
            budget: float = 90.0, force: bool = False) -> SyncState:
    """取密钥 + 增量解密到缓存目录。返回本次状态。"""
    storage = storage or (db_storages() or [None])[0]
    if storage is None:
        raise RuntimeError("没找到 xwechat_files/<账号>/db_storage；确认微信 4.x 已登录")
    cache = cache or DEFAULT_CACHE
    cache.mkdir(parents=True, exist_ok=True)

    state = SyncState(storage=storage, cache=cache, keys={})
    key_file = cache / "keys.json"
    cached = load_key_cache(key_file)

    # 库里现存的 salt，只关心这些
    wanted: dict[str, Path] = {}
    for db in sorted(storage.rglob("*.db")):
        try:
            if db.stat().st_size < PAGE_SZ:
                continue
            wanted[salt_of(db)] = db
        except OSError:
            continue
    state.keys = {salt: key for salt, key in cached.items() if salt in wanted}

    # 缺密钥 → 现场扫
    missing = set(wanted) - set(state.keys)
    if missing or force:
        hits = scan_keys(budget=budget)
        state.key_source = f"memory({len(hits)} 候选)"
        for hit in hits:
            if hit.salt_hex and hit.salt_hex in wanted:
                state.keys[hit.salt_hex] = hit.key_hex
        # 没有 salt 提示的，逐个库试
        loose = [(h.key_hex, bytes.fromhex(h.key_hex)) for h in hits if not h.salt_hex]
        for salt, db in wanted.items():
            if salt in state.keys or not loose:
                continue
            try:
                page1 = db.read_bytes()[:PAGE_SZ]
            except OSError:
                continue
            for key_hex, key_bytes in loose:
                if _verify(key_bytes, page1):
                    state.keys[salt] = key_hex
                    break
        key_file.write_text(json.dumps(state.keys, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    # 增量解密
    manifest_path = cache / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError):
        manifest = {}

    for salt, db in wanted.items():
        rel = str(db.relative_to(storage))
        key_hex = state.keys.get(salt)
        if not key_hex:
            state.failed.append(f"{rel}（没有密钥）")
            continue
        wal = db.with_name(db.name + "-wal")
        fingerprint = "+".join(
            f"{p.stat().st_size}@{int(p.stat().st_mtime)}"
            for p in (db, wal) if p.is_file())
        out = cache / rel
        if (not force and manifest.get(rel, {}).get("fingerprint") == fingerprint
                and out.is_file()):
            state.skipped += 1
            continue
        try:
            result = decrypt_database(db, out, key_hex, merge_wal=True)
        except Exception as exc:                    # noqa: BLE001
            state.failed.append(f"{rel}（{exc}）")
            continue
        if not result["ok"]:
            state.failed.append(f"{rel}（{result.get('reason')}）")
            continue
        manifest[rel] = {"fingerprint": fingerprint, "pages": result["pages"],
                         "wal_pages": result["wal_pages"],
                         "newest": _newest_in(out),
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        state.refreshed += 1

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return state


# --------------------------------------------------------------------------- #
# 4. 查询
# --------------------------------------------------------------------------- #
def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def md5_table(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def decompress(blob: Any, flag: Any) -> bytes:
    if blob in (None, "", b""):
        return b""
    if isinstance(blob, str):
        return blob.encode("utf-8", "replace")
    blob = bytes(blob)
    try:
        flag_int = int(flag) if flag is not None else None
    except (TypeError, ValueError):
        flag_int = None
    if blob[:4] == ZSTD_MAGIC or flag_int in ZSTD_FLAGS:
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(blob, max_output_size=64 << 20)
        except Exception:                            # noqa: BLE001
            try:
                import io
                import zstandard
                reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(blob))
                return reader.read()
            except Exception:                        # noqa: BLE001
                return b""
    return blob


def message_text(row: sqlite3.Row) -> str:
    keys = row.keys()
    raw = row["message_content"] if "message_content" in keys else None
    flag = row["WCDB_CT_message_content"] if "WCDB_CT_message_content" in keys else None
    if raw in (None, "", b"") and "compress_content" in keys:
        raw = row["compress_content"]
        flag = row["WCDB_CT_compress_content"] if "WCDB_CT_compress_content" in keys else 4
    data = decompress(raw, flag)
    return data.decode("utf-8", "replace") if data else ""


def base_type(local_type: Any) -> int:
    try:
        return int(local_type) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def canonical_url(url: str) -> str:
    """把带一堆转发参数的链接收敛成文章本体链接。

    微信转发会追加 mpshare/scene/srcid/sharer_shareinfo/… 和 #rd 锚点，
    这些是分享痕迹，不是文章身份。留下 __biz/mid/idx/sn 就够唯一定位。
    """
    if not url:
        return url
    url = url.replace("&amp;", "&")
    if "mp.weixin.qq.com" not in url:
        return url
    base, _, _fragment = url.partition("#")
    base = base.replace("http://", "https://", 1)
    head, _, query = base.partition("?")
    if not query:
        return base
    keep = ("__biz", "mid", "idx", "sn")
    params = []
    for part in query.split("&"):
        name = part.split("=", 1)[0]
        if name in keep and part not in params:
            params.append(part)
    return head + ("?" + "&".join(params) if params else "")


def article_of(text: str) -> dict[str, str] | None:
    """从公众号卡片 XML 里抠出文章信息。

    这里有个踩过的坑：``url`` 必须保留**原始完整链接**（含 ``chksm``／``mpshare``／
    ``scene``／``srcid``），只有 ``canonical`` 才做去参数化。
    微信现在会校验 ``chksm``：把参数裁成 ``__biz/mid/idx/sn`` 再去抓，服务器会
    判定为爬虫，返回腾讯云滑块验证页（17 KB），正文一个字都拿不到；保留原始参
    数则直接返回完整文章（3.6 MB）。所以"抓取用 url、去重用 canonical"必须分开。
    """
    if not text or "mp.weixin.qq.com" not in text:
        return None
    match = ARTICLE_URL.search(text)
    if not match:
        loose = re.search(r"https?://mp\.weixin\.qq\.com/s[^\s\"'<>\\\]]+", text)
        if not loose:
            return None
        url = loose.group(0)
    else:
        url = match.group(1)
    url = (url.replace("&amp;", "&").replace("&quot;", '"')
              .replace("&lt;", "<").replace("&gt;", ">").rstrip("&,;)"))
    title = TITLE_RE.search(text)
    des = DES_RE.search(text)
    nick = NICK_RE.search(text)
    return {
        "url": url,
        "canonical": canonical_url(url),
        "title": (title.group(1).strip() if title else ""),
        "digest": (des.group(1).strip()[:300] if des else ""),
        "account_nick": (nick.group(1).strip() if nick else ""),
    }


def account_names(cache: Path) -> dict[str, str]:
    path = cache / "contact" / "contact.db"
    names: dict[str, str] = {}
    if not path.is_file():
        return names
    con = _connect(path)
    try:
        for username, remark, nick, alias in con.execute(
                "SELECT username, remark, nick_name, alias FROM contact"):
            names[username] = remark or nick or alias or username
        try:
            for username, info in con.execute(
                    "SELECT username, brand_info FROM biz_info WHERE brand_info IS NOT NULL"):
                if username in names and names[username] == username and info:
                    try:
                        brand = json.loads(info)
                        label = brand.get("nick_name") or brand.get("brand_name")
                        if label:
                            names[username] = label
                    except (ValueError, TypeError):
                        pass
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return names


def iter_recent(cache: Path, db_rel: str, limit: int = 500,
                since: int | None = None) -> Iterator[tuple[str, sqlite3.Row]]:
    """遍历某个库所有会话表里最近的消息，产出 (user_name, row)。"""
    path = cache / db_rel
    if not path.is_file():
        return
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        mapping: dict[str, str] = {}
        try:
            for username, in con.execute("SELECT user_name FROM Name2Id"):
                if username:
                    mapping[md5_table(username)] = username
        except sqlite3.Error:
            pass
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        for table in tables:
            username = mapping.get(table)
            if not username:
                continue
            sql = f'SELECT * FROM "{table}"'
            params: list[Any] = []
            if since:
                sql += " WHERE create_time >= ?"
                params.append(int(since))
            sql += " ORDER BY create_time DESC LIMIT ?"
            params.append(int(limit))
            try:
                for row in con.execute(sql, params):
                    yield username, row
            except sqlite3.Error:
                continue
    finally:
        con.close()


@dataclass
class Article:
    create_time: int
    account: str
    account_id: str
    title: str
    url: str
    digest: str
    via: str            # official=公众号推送 / self=自己转发 / chat=聊天里转的

    def as_dict(self) -> dict[str, Any]:
        return {"create_time": self.create_time,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.create_time)),
                "account": self.account, "account_id": self.account_id,
                "title": self.title, "url": self.url, "digest": self.digest,
                "via": self.via}


SELF_SESSIONS = ("filehelper",)


def collect_articles(cache: Path | None = None, days: int = 7,
                     want_self: bool = True) -> list[Article]:
    """公众号推文 + 自己转发的链接，按时间倒序。"""
    cache = cache or DEFAULT_CACHE
    since = int(time.time() - days * 86400)
    names = account_names(cache)
    me = _self_username(cache)
    out: list[Article] = []
    seen: set[str] = set()

    for username, row in iter_recent(cache, "message/biz_message_0.db", limit=400, since=since):
        if base_type(row["local_type"]) != 49:
            continue
        text = message_text(row)
        article = article_of(text)
        if not article:
            continue
        if article["canonical"] in seen:
            continue
        seen.add(article["canonical"])
        out.append(Article(int(row["create_time"] or 0), names.get(username, username),
                           username, article["title"], article["url"],
                           article["digest"], "official"))

    if want_self:
        for username, row in iter_recent(cache, "message/message_0.db", limit=400,
                                         since=since):
            if base_type(row["local_type"]) != 49:
                continue
            is_self = username == me or username in SELF_SESSIONS
            if not is_self:
                continue
            text = message_text(row)
            article = article_of(text)
            if not article or article["canonical"] in seen:
                continue
            seen.add(article["canonical"])
            out.append(Article(int(row["create_time"] or 0), "（我自己）", username,
                               article["title"], article["url"], article["digest"], "self"))
    out.sort(key=lambda item: item.create_time, reverse=True)
    return out


def _self_username(cache: Path) -> str:
    """自己的 wxid：db_storage 的父目录名去掉 4.x 的 ``_NNNN`` 后缀。"""
    try:
        account_dir = (cache / "manifest.json").parent
        for storage in db_storages():
            name = storage.parent.name
            base = name.rsplit("_", 1)[0] if "_" in name else name
            if base.startswith(("wxid_", "gh_")):
                return base
        return ""
    except Exception:                                # noqa: BLE001
        return ""


def collect_links(cache: Path | None = None, days: int = 7,
                  include_all: bool = True) -> list[Article]:
    """所有会话里出现的 mp.weixin.qq.com 链接（含别人转给我的）。"""
    cache = cache or DEFAULT_CACHE
    since = int(time.time() - days * 86400)
    names = account_names(cache)
    me = _self_username(cache)
    out: list[Article] = []
    seen: set[str] = set()
    for db_rel in ("message/message_0.db", "message/biz_message_0.db"):
        for username, row in iter_recent(cache, db_rel, limit=400, since=since):
            text = message_text(row)
            if not text or "mp.weixin.qq.com" not in text:
                continue
            article = article_of(text)
            if not article or article["canonical"] in seen:
                continue
            seen.add(article["canonical"])
            via = "self" if (username == me or username in SELF_SESSIONS) else (
                "official" if db_rel.endswith("biz_message_0.db") else "chat")
            out.append(Article(int(row["create_time"] or 0), names.get(username, username),
                               username, article["title"], article["url"],
                               article["digest"], via))
    out.sort(key=lambda item: item.create_time, reverse=True)
    return out


def quick_status() -> dict[str, Any]:
    pids = weixin_pids()
    storages = db_storages()
    count = sum(1 for _ in (storages[0].rglob("*.db")) if _) if storages else 0
    return {"running": bool(pids), "pids": pids,
            "storages": [str(path) for path in storages], "databases": count}


# --------------------------------------------------------------------------- #
# 5. 给雷达用的门面
# --------------------------------------------------------------------------- #
def available() -> bool:
    """4.x 通道能不能用。不碰内存。

    两条成立条件，满足任意一条就够：

    1. Weixin.exe 在跑（能取到新密钥）；
    2. 之前已经取到过密钥、也有解密缓存——这时微信关着也能继续读"最后一次同步
       到的内容"，库文件不再被占用，反而更好读。只把"必须有进程"当门槛会让
       用户一关微信，整个来源就开始报错。
    """
    status = quick_status()
    if status["running"] and status["storages"]:
        return True
    cache = DEFAULT_CACHE
    if (cache / "keys.json").is_file():
        try:
            return any(path.is_file() for path in cache.rglob("*.db"))
        except OSError:
            return False
    return False


def oa_directory(cache: Path | None = None) -> dict[str, dict[str, Any]]:
    """公众号目录：user_name -> {name, aliases}，给名字匹配用。"""
    cache = cache or DEFAULT_CACHE
    path = cache / "contact" / "contact.db"
    if not path.is_file():
        return {}
    result: dict[str, dict[str, Any]] = {}
    con = _connect(path)
    try:
        for username, remark, nick, alias in con.execute(
                "SELECT username, remark, nick_name, alias FROM contact "
                "WHERE username LIKE 'gh_%'"):
            aliases = {_name_key(value) for value in (nick, remark, alias) if value}
            result[str(username)] = {"name": str(remark or nick or alias or username),
                                     "aliases": aliases}
    finally:
        con.close()
    return result


def _name_key(value: Any) -> str:
    import unicodedata
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def account_rows(cache: Path | None = None, limit: int = 20000) -> list[dict[str, Any]]:
    """本机微信里"我关注的全部公众号"清单，用来一次性灌进公众号库。

    新装的环境里库里必须是**全量、且一个都不追踪**：让用户自己在网页上勾。
    所以这里只负责如实读出来，``enabled`` 一律交给调用方置 0。

    名字优先级：备注 > 品牌昵称 > 昵称 > 微信号原始 id。备注是用户自己起的名，
    最贴近他在微信里看到的样子；``gh_xxx`` 原始 id 放最后只是兜底。
    """
    cache = cache or DEFAULT_CACHE
    path = cache / "contact" / "contact.db"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    con = _connect(path)
    try:
        brand: dict[str, str] = {}
        try:
            for username, info in con.execute(
                    "SELECT username, brand_info FROM biz_info WHERE brand_info IS NOT NULL"):
                if not info:
                    continue
                try:
                    payload = json.loads(info)
                except (ValueError, TypeError):
                    continue
                label = (payload.get("nick_name") or payload.get("brand_name")
                         or payload.get("alias") or "")
                if label:
                    brand[str(username)] = str(label)
        except sqlite3.Error:
            brand = {}
        for username, remark, nick, alias in con.execute(
                "SELECT username, remark, nick_name, alias FROM contact "
                "WHERE username LIKE 'gh_%' ORDER BY username LIMIT ?", (int(limit),)):
            username = str(username)
            name = (remark or brand.get(username) or nick or alias or username).strip()
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            aliases = sorted({value for value in (remark, nick, alias, brand.get(username))
                              if value and str(value).strip()})
            rows.append({
                "name": name,
                "account_id": username,
                "alias": str(alias or ""),
                "description": (f"微信号 {alias}" if alias else "") or username,
                "aliases": aliases,
            })
    finally:
        con.close()
    return rows


def official_articles(since_ts: int | None = None, per_account: int = 300,
                      cache: Path | None = None) -> list[dict[str, Any]]:
    """公众号（biz_message_0.db）里指定时间之后的文章卡片。"""
    cache = cache or DEFAULT_CACHE
    names = account_names(cache)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for username, row in iter_recent(cache, "message/biz_message_0.db",
                                     limit=per_account, since=since_ts):
        if base_type(row["local_type"]) != 49:
            continue
        article = article_of(message_text(row))
        if not article or article["canonical"] in seen:
            continue
        seen.add(article["canonical"])
        out.append({
            "user_name": username,
            "account": names.get(username, username),
            "create_time": int(row["create_time"] or 0),
            "local_id": int(row["local_id"] or 0),
            "server_id": str(row["server_id"] or ""),
            "title": article["title"],
            "url": article["url"],
            "digest": article["digest"],
            "via": "official",
        })
    out.sort(key=lambda item: item["create_time"], reverse=True)
    return out


def self_links(since_ts: int | None = None, per_chat: int = 300,
               cache: Path | None = None) -> list[dict[str, Any]]:
    """自己给自己（含文件传输助手）转发的文章卡片。"""
    cache = cache or DEFAULT_CACHE
    names = account_names(cache)
    me = _self_username(cache)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for username, row in iter_recent(cache, "message/message_0.db",
                                     limit=per_chat, since=since_ts):
        if base_type(row["local_type"]) != 49:
            continue
        if username != me and username not in SELF_SESSIONS:
            continue
        article = article_of(message_text(row))
        if not article or article["canonical"] in seen:
            continue
        seen.add(article["canonical"])
        out.append({
            "user_name": username,
            "account": names.get(username, username),
            "create_time": int(row["create_time"] or 0),
            "local_id": int(row["local_id"] or 0),
            "server_id": str(row["server_id"] or ""),
            "title": article["title"],
            "url": article["url"],
            "digest": article["digest"],
            "via": "self",
        })
    out.sort(key=lambda item: item["create_time"], reverse=True)
    return out


def cached_newest(cache: Path | None = None) -> int:
    """缓存清单里记录的最新消息时间戳（增量窗口的下界）。"""
    cache = cache or DEFAULT_CACHE
    try:
        manifest = json.loads((cache / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError):
        return 0
    stamps = [int(item.get("newest") or 0) for item in manifest.values()
              if isinstance(item, dict)]
    return max(stamps) if stamps else 0


def _newest_in(path: Path) -> int:
    """一个库里所有会话表的最新 create_time。"""
    try:
        con = _connect(path)
    except sqlite3.Error:
        return 0
    newest = 0
    try:
        for (table,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
            try:
                value = con.execute(f'SELECT max(create_time) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                continue
            if value and int(value) > newest:
                newest = int(value)
    finally:
        con.close()
    return newest


# --------------------------------------------------------------------------- #
# CLI（诊断用）
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    if command == "status":
        print(json.dumps(quick_status(), ensure_ascii=False, indent=2))
        return 0
    if command in ("refresh", "sync"):
        state = refresh(force="--force" in argv)
        print(f"数据目录: {state.storage}")
        print(f"缓存目录: {state.cache}")
        print(f"密钥来源: {state.key_source or '缓存'}")
        print(f"更新 {state.refreshed} 个库，跳过 {state.skipped} 个，失败 {len(state.failed)}")
        for line in state.failed:
            print("  -", line)
        return 0 if not state.failed else 1
    if command == "articles":
        days = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 7
        items = collect_articles(days=days)
        print(f"== 公众号推文 / 自己转发（近 {days} 天，共 {len(items)} 条）==")
        for item in items[:40]:
            print(f"[{item.as_dict()['time']}] ({item.via}) {item.account}")
            print(f"   {item.title}")
            print(f"   {item.url}")
        return 0
    if command == "links":
        days = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 7
        items = collect_links(days=days)
        print(f"== 微信里的公众号链接（近 {days} 天，共 {len(items)} 条）==")
        for item in items[:40]:
            print(f"[{item.as_dict()['time']}] ({item.via}) {item.account}")
            print(f"   {item.title}")
            print(f"   {item.url}")
        return 0
    if command == "accounts":
        rows = account_rows()
        print(f"== 本机关注的公众号 {len(rows)} 个（默认全部不追踪）==")
        for row in rows[:40]:
            print(f"  {row['name'][:30]:32} {row['account_id']}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
