"""微信 4.x 订阅号盒子：直接从 Weixin.exe 的内存里读推送列表。

为什么需要它
------------
4.x 把订阅号折叠进「订阅号消息」盒子：单个 gh_ 会话不再更新，正文也不写进
``biz_message_*.db``（本机实测 4.x 里有正文表的只有 25 个服务号/工具号，追踪
名单内 0 个）。盒子里的推送只留在两处：

1. ``session.db`` 的 ``brandsessionholder``：只有**一条**最新摘要；
2. 客户端内存：盒子卡片的 JSON 列表，每条带 ``AppMsgId``（mid）、``DateTime``、
   ``uniqueId``（``gh_xxx@mid``）和完整 ``Title``；另有文章 URL 池，按 mid 对齐。

2026-11-18 实测：一次扫描 0.8 秒，拿到 78 个号各一条最新推送（时间跨 24 小时），
2312 个 URL，mid 与 URL 的匹配率 16/16。这是 4.x 下唯一能恢复「自动读取所有
公众号更新」的本地路径——不需要点开任何推文，不需要外部服务。

只读、只读、只读：OpenProcess + ReadProcessMemory，绝不写微信任何文件。
"""
from __future__ import annotations

import ctypes
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..utils import canonical_url

IS_WINDOWS = sys.platform == "win32"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

# 盒子卡片：{"AppMsgId":2247487245,"DateTime":1794968546,"weight":200,
#            "uniqueId":"gh_b454d3a6ed68@2247487245","localId":...,"Title":"…"}
CARD_RE = re.compile(
    rb'\{"AppMsgId":(\d+|"[^"]*"),"DateTime":(\d+),"weight":\d+,'
    rb'"uniqueId":"([^"@]*)@([^"]*)","localId":(\d+),"Title":"((?:[^"\\]|\\.)*)"\}')
# 号信息：{"…","showName":"纳米孔测序圈子","sourceUsername":"gh_b454d3a6ed68",…}
ACC_RE = re.compile(
    rb'"showName":"((?:[^"\\]|\\.)*)","sourceUsername":"(gh_[0-9a-z]+)"')
URL_RE = re.compile(rb"https?://mp\.weixin\.qq\.com/s[^\s\"'<>\\\x00]{10,400}")
MID_IN_URL = re.compile(r"[?&]mid=(\d+)")

READ_CHUNK = 8 << 20          # 单次最多读 8 MB
SCAN_BUDGET = 20.0            # 一次扫描最多花 20 秒


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong), ("RegionSize", ctypes.c_size_t),
                ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong)]


@dataclass
class Snapshot:
    at: float
    cards: list[dict[str, Any]] = field(default_factory=list)
    accounts: dict[str, str] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)   # mid -> 完整 URL
    scanned_mb: float = 0.0
    elapsed: float = 0.0
    reason: str = ""                                      # 不可用时的原因

    @property
    def ok(self) -> bool:
        return not self.reason

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "cards": len(self.cards), "accounts": len(self.accounts),
                "urls": len(self.urls), "scanned_mb": round(self.scanned_mb, 1),
                "elapsed": round(self.elapsed, 2), "reason": self.reason}


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def weixin_pid() -> int:
    """内存占用最大的 Weixin.exe。走 psutil，本机 tasklist 被安全策略拦掉。"""
    try:
        import psutil
    except Exception:                                     # noqa: BLE001
        return 0
    best: tuple[int, int] = (0, 0)
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower() != "weixin.exe":
                continue
            rss = proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        if rss > best[1]:
            best = (proc.pid, rss)
    return best[0]


def _regions(handle: int) -> Iterator[tuple[int, int]]:
    """只产出 private、可读、非 guard 的区域——盒子数据一定在堆里。"""
    k32 = _kernel32()
    address, info = 0, _MBI()
    while k32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info),
                             ctypes.sizeof(info)):
        base, size = int(info.BaseAddress or 0), int(info.RegionSize or 0)
        if size <= 0:
            break
        if (info.State == MEM_COMMIT and info.Type == MEM_PRIVATE
                and info.Protect in READABLE
                and not (info.Protect & PAGE_NOACCESS) and not (info.Protect & PAGE_GUARD)):
            yield base, size
        address = base + size


def _read(handle: int, address: int, size: int) -> bytes:
    if size <= 0 or address <= 0:
        return b""
    k32 = _kernel32()
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size,
                             ctypes.byref(got)) and got.value:
        return buffer.raw[:got.value]
    return b""


def _decode_json_string(raw: bytes) -> str:
    """把 JSON 里的转义串还原成正常文本。"""
    try:
        return json.loads(b'"' + raw + b'"').decode("utf-8", "replace")
    except Exception:                                     # noqa: BLE001
        return raw.decode("utf-8", "replace")


def available() -> bool:
    return IS_WINDOWS and bool(weixin_pid())


def scan(budget: float = SCAN_BUDGET) -> Snapshot:
    """扫一次微信进程内存，返回盒子卡片 + 号名 + URL 池。

    只读 private 堆。本机实测 405 MB / 0.8 秒。
    """
    now = time.time()
    if not IS_WINDOWS:
        return Snapshot(at=now, reason="只有 Windows 才有微信内存通道")
    pid = weixin_pid()
    if not pid:
        return Snapshot(at=now, reason="Weixin.exe 没有运行")
    h = _kernel32().OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return Snapshot(at=now, reason="打不开 Weixin.exe 进程（权限不足或已退出）")

    cards: dict[str, dict[str, Any]] = {}
    accounts: dict[str, str] = {}
    mid_url: dict[str, str] = {}
    scanned = 0
    t0 = time.time()
    try:
        for base, size in _regions(h):
            off = 0
            while off < size:
                chunk = min(READ_CHUNK, size - off)
                data = _read(h, base + off, chunk)
                off += chunk
                scanned += len(data)
                if data:
                    if b'"AppMsgId"' in data:
                        for m in CARD_RE.finditer(data):
                            gh = m.group(3).decode("ascii", "replace")
                            if not gh.startswith("gh_"):
                                continue
                            mid = m.group(4).decode("ascii", "replace")
                            try:
                                ts = int(m.group(2))
                            except ValueError:
                                continue
                            if ts <= 0:
                                continue
                            key = f"{gh}@{mid}"
                            prev = cards.get(key)
                            title = _decode_json_string(m.group(6))
                            if prev is None or (not prev["title"] and title):
                                cards[key] = {"gh": gh, "mid": mid, "ts": ts,
                                              "title": title, "key": key}
                    if b'"showName"' in data:
                        for m in ACC_RE.finditer(data):
                            accounts.setdefault(m.group(2).decode("ascii", "replace"),
                                                _decode_json_string(m.group(1)))
                    if b"mp.weixin.qq.com" in data:
                        for m in URL_RE.finditer(data):
                            url = m.group(0).decode("utf-8", "replace")
                            hit = MID_IN_URL.search(url)
                            if hit:
                                mid_url.setdefault(hit.group(1), url)
            if time.time() - t0 > budget:
                break
    finally:
        try:
            _kernel32().CloseHandle(h)
        except Exception:                             # noqa: BLE001
            pass

    # 把 URL 贴回卡片：mid 是唯一能对上号的东西
    out: list[dict[str, Any]] = []
    for card in cards.values():
        card["url"] = mid_url.get(card["mid"], "")
        card["canonical_url"] = canonical_url(card["url"]) if card["url"] else ""
        out.append(card)
    out.sort(key=lambda c: c["ts"], reverse=True)
    return Snapshot(at=now, cards=out, accounts=accounts, urls=mid_url,
                    scanned_mb=scanned / 1048576, elapsed=time.time() - t0)
