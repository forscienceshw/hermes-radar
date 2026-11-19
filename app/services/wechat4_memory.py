"""微信 4.x 订阅号盒子：直接从 Weixin.exe 的内存里读推送列表。

为什么需要它
------------
4.x 把订阅号折叠进「订阅号消息」盒子：单个 gh_ 会话不再更新，正文也不写进
``biz_message_*.db``（本机实测 4.x 里有正文表的只有 25 个服务号/工具号，追踪
名单内 0 个）。盒子里的推送只留在两处：

1. ``session.db`` 的 ``brandsessionholder``：只有**一条**最新摘要；
2. 客户端内存：盒子卡片的 JSON 列表，每条带 ``AppMsgId``（mid）、``DateTime``、
   ``uniqueId``（``gh_xxx@mid``）和完整 ``Title``；另有文章 URL 池，按 mid 对齐。

2026-11-18 实测：一次扫描 1.5 秒 / 582 MB，拿到 **225 条卡片（193 个号各一条
最新推送，时间跨 36 小时）**，其中 190 条带完整正文链接；URL 池 1518 条。
只认单一 JSON 形态时同样一次扫描只有 84 张卡片、31 条带链接——差的就是下面
那两种形态要不要合起来用。

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
# 盒子里的数据有两种形态，必须合起来用：
#   A 精简形态：{"AppMsgId":mid,"DateTime":ts,"weight":…,"uniqueId":"gh_x@mid",…,"Title":"…"}
#     —— 有标题和 gh，**没有 URL**；
#   B 完整形态：{"AppMsgId":mid,"DateTime":ts,"isPreview":…,"detailInfo":{…,"ContentUrl":"…"},
#               "showName":"…","sourceUsername":"gh_x",…}
#     —— 有 URL 和号名，字段顺序不固定。
# 2026-11-18 实测：A 形态 231 条、B 形态约 1500 条 URL，两者按 mid 对齐后有
# 178 条三者齐全；而只认 A 形态的旧写法一次只拿到 84 张卡片。
UNIQ_RE = re.compile(rb'"uniqueId":"(gh_[0-9a-z]+)@(\d+)"')
HEAD_RE = re.compile(rb'"AppMsgId":(\d{5,}),"DateTime":(\d{9,})')
TITLE_RE = re.compile(rb'"Title":"((?:[^"\\]|\\.)*)"')
OBJ_SPAN = 8000               # 一个元素最多往后看 8 KB 找 Title
# 号信息：{"…","showName":"纳米孔测序圈子","sourceUsername":"gh_b454d3a6ed68",…}
ACC_RE = re.compile(
    rb'"showName":"((?:[^"\\]|\\.)*)","sourceUsername":"(gh_[0-9a-z]+)"')
# 只收可打印字符：进程堆里 URL 常常紧挨着二进制字段，以前放过 \x02 这类
# 控制字符，结果归档时 httpx 直接抛 "Invalid non-printable ASCII character"，
# 条目被记成失败。正则截断在控制字符处，剩下的前缀仍是能打开的链接
# （__biz + mid + idx + sn 齐了就够，尾部 chksm 掉了不影响）。
URL_RE = re.compile(rb"https?://mp\.weixin\.qq\.com/s[^\x00-\x20\"'<>\\\x7f]{10,400}")
MID_IN_URL = re.compile(r"[?&]mid=(\d+)")
# 同一次群发推多篇时 mid 相同、idx 不同（idx=1 是靠上那篇）。以前只按 mid
# 归并，第二篇起全部被 setdefault 吞掉 —— 用户看到的就是"同时推两篇，只读到
# 上面那篇"。2026-11-19 实测进程堆里有 101 个 mid 带多个 idx（最多 4 篇）。
IDX_IN_URL = re.compile(r"[?&]idx=(\d+)")

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
    """把 JSON 里的转义串还原成正常文本。

    注意 ``json.loads`` 拿到 bytes 时返回的已经是 **str**，不能再 ``.decode()``；
    以前多调了一次，异常被吞掉后总是走 fallback，于是 ``\\u7b2c`` 这种转义写法
    的标题一直原样带着反斜杠进库。
    """
    try:
        value = json.loads(b'"' + raw + b'"')
        if isinstance(value, str):
            return value
    except Exception:                                     # noqa: BLE001
        pass
    return raw.decode("utf-8", "replace")


def _url_variants(value: Any) -> dict[int, str]:
    """把 URL 池的一项拆成 {idx: url}，兼容旧的"mid→单个 URL"写法。"""
    if isinstance(value, dict):
        out: dict[int, str] = {}
        for key, url in value.items():
            try:
                out[int(key)] = str(url)
            except (TypeError, ValueError):
                continue
        return {k: v for k, v in out.items() if v}
    if isinstance(value, str) and value:
        return {1: value}
    return {}


def _titles_of(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in (value or []) if item]


def _url_score(url: str) -> tuple:
    """挑最干净的那条链接。

    同一篇的 URL 在堆里会有多个变体：http / https 并存，尾部粘着二进制
    （表现为 U+FFFD），或者被截断丢了 sn。干净的优先，短一点说明没粘垃圾。
    """
    return ("\ufffd" not in url, url.startswith("https://"), "sn=" in url, -len(url))


def _assemble_cards(gh_of: dict[str, str], ts_of: dict[str, int],
                    title_of: dict[str, Any], mid_url: dict[str, Any]) -> list[dict[str, Any]]:
    """把三种来源按 (mid, idx) 归并成卡片。

    gh 来自 A 形态的 ``uniqueId``（精确配对，不看字段顺序），标题来自 A 形态的
    ``Title``，URL 来自 B 形态的 ``ContentUrl``（URL 自带 mid/idx，所以对得上）。
    缺哪个就先空着，下一轮扫描补——不要因为缺 URL 就把整条丢掉。

    **一次群发多篇时 mid 相同、idx 不同**，所以要按 (mid, idx) 出卡片：key 用
    ``gh@mid``（idx=1，保持与历史数据一致）或 ``gh@mid#idx``。
    """
    out: list[dict[str, Any]] = []
    for mid, gh in gh_of.items():
        titles = _titles_of(title_of.get(mid))
        variants = _url_variants(mid_url.get(mid))
        ts = ts_of.get(mid, 0)
        if not variants:
            if not titles:
                continue                               # 什么都没有，收了也没用
            out.append({"gh": gh, "mid": mid, "idx": 1, "ts": ts, "title": titles[0],
                        "key": f"{gh}@{mid}", "url": "", "canonical_url": ""})
            continue
        for idx in sorted(variants):
            url = variants[idx]
            if idx == 1:
                title = titles[0] if titles else ""
            elif len(titles) >= idx:
                # 标题的顺序是堆里的出现顺序，只有数量够时才敢按序号配；
                # 不够就留空（入库时用 URL 兜底，归档后会以正文标题为准）
                title = titles[idx - 1]
            else:
                title = ""
            out.append({"gh": gh, "mid": mid, "idx": idx, "ts": ts, "title": title,
                        "key": f"{gh}@{mid}" if idx == 1 else f"{gh}@{mid}#{idx}",
                        "url": url, "canonical_url": canonical_url(url) if url else ""})
    out.sort(key=lambda c: (c["ts"], -c["idx"]), reverse=True)
    return out


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

    gh_of: dict[str, str] = {}          # mid -> gh（uniqueId 精确配对，不靠字段顺序）
    ts_of: dict[str, int] = {}
    title_of: dict[str, list[str]] = {}  # mid -> 多个标题（一次群发多篇时按序对应 idx）
    accounts: dict[str, str] = {}
    mid_url: dict[str, dict[int, str]] = {}   # mid -> {idx: url}
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
                    if b'"uniqueId"' in data:
                        for m in UNIQ_RE.finditer(data):
                            gh_of.setdefault(m.group(2).decode("ascii", "replace"),
                                             m.group(1).decode("ascii", "replace"))
                    if b'"AppMsgId"' in data:
                        # 逐个元素解析：窗口截止到下一个 AppMsgId，避免把邻居的
                        # Title 认成自己的（堆里同一份 JSON 会被复制很多遍）。
                        heads = list(HEAD_RE.finditer(data))
                        for i, head in enumerate(heads):
                            end = heads[i + 1].start() if i + 1 < len(heads) else len(data)
                            mid = head.group(1).decode("ascii", "replace")
                            try:
                                ts_of.setdefault(mid, int(head.group(2)))
                            except ValueError:
                                pass
                            window = data[head.start():min(end, head.start() + OBJ_SPAN)]
                            m = TITLE_RE.search(window)
                            if m:
                                title = _decode_json_string(m.group(1))
                                # 堆里的截断片段常带 U+FFFD，宁可留空也别拿它当标题
                                if title and "\ufffd" not in title:
                                    names = title_of.setdefault(mid, [])
                                    if title not in names and len(names) < 8:
                                        names.append(title)
                    if b'"showName"' in data:
                        for m in ACC_RE.finditer(data):
                            accounts.setdefault(m.group(2).decode("ascii", "replace"),
                                                _decode_json_string(m.group(1)))
                    if b"mp.weixin.qq.com" in data:
                        for m in URL_RE.finditer(data):
                            url = m.group(0).decode("utf-8", "replace")
                            hit = MID_IN_URL.search(url)
                            if not hit:
                                continue
                            idx_hit = IDX_IN_URL.search(url)
                            try:
                                idx = int(idx_hit.group(1)) if idx_hit else 1
                            except ValueError:
                                idx = 1
                            bucket = mid_url.setdefault(hit.group(1), {})
                            current = bucket.get(idx)
                            if current is None or _url_score(url) > _url_score(current):
                                bucket[idx] = url
            if time.time() - t0 > budget:
                break
    finally:
        try:
            _kernel32().CloseHandle(h)
        except Exception:                             # noqa: BLE001
            pass

    # 拼卡片：gh 来自 A 形态的 uniqueId，标题来自 A 形态的 Title，URL 来自 B 形态的
    # ContentUrl（URL 自己带 mid，所以对得上）。三者按 mid 归并，缺哪个先空着，
    # 下一轮扫描补。
    out = _assemble_cards(gh_of, ts_of, title_of, mid_url)
    flat_urls = {f"{mid}#{idx}": url for mid, bucket in mid_url.items()
                 for idx, url in bucket.items()}
    return Snapshot(at=now, cards=out, accounts=accounts, urls=flat_urls,
                    scanned_mb=scanned / 1048576, elapsed=time.time() - t0)
