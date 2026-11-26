"""WorkBuddy 点数直连通道：用客户端自己的登录态调 ``/v2/chat/completions``。

为什么需要它
------------
官方云 SDK（``cloud/dist/llm-worker.mjs``）在 Node 里没有 localStorage，session
只能退回内存，每次都是匿名身份。2026-11-20 17:40 起网关对该应用返回
``401 login_required``，匿名通道整体关闭——那时起所有精读都失败。

客户端自己不受影响：它持有 CodeBuddy 账号的 access token（Keycloak 签发，
``aud=account``，本机实测有效期 28 天），并用它直连
``https://www.workbuddy.cn/v2/chat/completions``。这个 token 在 Weixin.exe
那套只读手段下同样能取到：OpenProcess + ReadProcessMemory 扫 private 堆里的
JWT 串，解 payload 拿 ``exp``，缓存到进程内存。

所以这里的做法是**借用客户端已经登录好的身份**，而不是重新登录一次：
不写客户端任何文件，不碰注册表，不要求用户点任何东西。客户端没运行时读不到
token，就如实报错并回退 SDK（此时 SDK 也是 401，错误会一起带出来）。

只读、只读、只读：OpenProcess + ReadProcessMemory，外加走到网关的 HTTPS 请求。
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Any, Callable

from ..paths import DATA_DIR

GATEWAY_URL = "https://www.workbuddy.cn/v2/chat/completions"
PROCESS_NAME = "WorkBuddy.exe"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
READ_CHUNK = 1 << 20
SCAN_BUDGET_BYTES = 900 * 1024 * 1024

JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

TOKEN_CACHE = DATA_DIR / "token-cache.bin"

# 进程内缓存：token 只在内存里，不落盘。exp 前 5 分钟视为过期，重扫一次。
_CACHE: dict[str, Any] = {"token": "", "exp": 0.0}


class DirectUnavailable(RuntimeError):
    """拿不到客户端令牌（客户端没开、不是 Windows、或内存里没找到）。"""


def _kernel32():
    if sys.platform != "win32":
        raise DirectUnavailable("只有 Windows 才能借用客户端登录态")
    return ctypes.WinDLL("kernel32", use_last_error=True)


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


def _client_pids_psutil() -> list[tuple[int, int]] | None:
    """用 psutil 取进程，一个子进程都不起；psutil 不可用时返回 None。

    以前这里跑 ``tasklist``。图形宿主（pythonw / --windowed）没有控制台，Windows
    会给控制台子进程**新建一个黑框**，用户就看到命令行一闪。能不出进程就不出。
    """
    try:
        import psutil
    except ImportError:
        return None
    found: list[tuple[int, int]] = []
    try:
        processes = psutil.process_iter(["name", "memory_info"])
    except (psutil.Error, OSError):
        return None
    for proc in processes:
        try:
            info = proc.info
            name = str(info.get("name") or "")
            if name.lower() != PROCESS_NAME.lower():
                continue
            memory = info.get("memory_info")
            found.append((proc.pid, int(getattr(memory, "rss", 0) or 0)))
        except (psutil.Error, OSError):
            # 枚举过程中有进程退出、或系统进程不给看，跳过就行。
            continue
    return found


def _client_pids_tasklist() -> list[tuple[int, int]]:
    """psutil 缺席时的兜底：tasklist 必须关窗，否则黑框一闪。"""
    from ..winproc import run_hidden

    try:
        out = run_hidden(["tasklist", "/FI", f"IMAGENAME eq {PROCESS_NAME}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="ignore", timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[tuple[int, int]] = []
    for line in (out or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 5 or not parts[0].lower().endswith(PROCESS_NAME.lower()):
            continue
        try:
            pid = int(parts[1])
            mem = int(re.sub(r"[^0-9]", "", parts[4]) or 0)
        except (ValueError, IndexError):
            continue
        found.append((pid, mem))
    return found


def client_pids() -> list[int]:
    """按内存占用从大到小列出 WorkBuddy.exe 的 PID。"""
    if sys.platform != "win32":
        return []
    found = _client_pids_psutil()
    if found is None:
        found = _client_pids_tasklist()
    found.sort(key=lambda item: -item[1])
    return [pid for pid, _ in found]


def _regions(handle: int, kernel32) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    address = 0
    info = _MemoryBasicInformation()
    while len(out) < 40000:
        got = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info),
                                      ctypes.sizeof(info))
        if not got:
            break
        base = info.BaseAddress or 0
        size = int(info.RegionSize or 0)
        if not size:
            break
        if (info.State == MEM_COMMIT and info.Type == MEM_PRIVATE
                and info.Protect in READABLE and not info.Protect & PAGE_GUARD):
            out.append((base, size))
        address = base + size
    return out


def _read(handle: int, address: int, size: int, kernel32) -> bytes:
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, size, ctypes.byref(read))
    return buf.raw[: read.value] if ok else b""


def _decode_part(part: str) -> dict[str, Any]:
    padded = part + "=" * (-len(part) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def token_expiry(token: str) -> float:
    """JWT 的 exp（秒）。解不出来就当 0，调用方会跳过它。"""
    parts = token.split(".")
    if len(parts) != 3:
        return 0.0
    payload = _decode_part(parts[1])
    try:
        return float(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return 0.0


def token_audience(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    aud = _decode_part(parts[1]).get("aud")
    if isinstance(aud, list):
        return ",".join(str(x) for x in aud)
    return str(aud or "")


def scan_tokens(pid: int) -> list[str]:
    """在某个进程的 private 堆里扫 JWT 候选。"""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return []
    seen: dict[str, None] = {}
    scanned = 0
    try:
        for base, size in _regions(handle, kernel32):
            offset = 0
            while offset < size:
                chunk = min(READ_CHUNK, size - offset)
                data = _read(handle, base + offset, chunk, kernel32)
                offset += chunk
                scanned += len(data)
                if data and b"eyJ" in data:
                    for match in JWT_RE.finditer(data):
                        seen.setdefault(match.group(0).decode("ascii", "replace"), None)
                if scanned >= SCAN_BUDGET_BYTES:
                    return list(seen)
    finally:
        kernel32.CloseHandle(handle)
    return list(seen)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """用当前 Windows 账户加密/解密。换机器或换用户都解不开，等于本地自毁。"""
    if sys.platform != "win32":
        raise OSError("DPAPI 只在 Windows 上可用")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = call(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError(f"DPAPI 调用失败（{ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _save_cached_token(token: str, exp: float) -> None:
    """把令牌加密存一份，客户端关掉时还能用（有效期 28 天，不会立刻失效）。"""
    try:
        blob = json.dumps({"token": token, "exp": exp}).encode("utf-8")
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_bytes(_dpapi(blob, True))
    except Exception:                      # noqa: BLE001 - 存不下来不影响本次调用
        pass


def _load_cached_token() -> tuple[str, float]:
    try:
        data = json.loads(_dpapi(TOKEN_CACHE.read_bytes(), False))
        return str(data.get("token") or ""), float(data.get("exp") or 0)
    except Exception:                      # noqa: BLE001 - 没缓存/解不开都是"没有"
        return "", 0.0


def client_token(force: bool = False) -> str:
    """取客户端当前的 access token；进程内存优先，其次本地加密副本。"""
    now = time.time()
    cached = _CACHE.get("token") or ""
    if cached and not force and float(_CACHE.get("exp") or 0) > now + 300:
        return cached

    best, best_exp = "", 0.0
    for pid in client_pids():
        for token in scan_tokens(pid):
            exp = token_expiry(token)
            if exp <= now + 60:
                continue
            # aud=account 是账号级令牌；别的（比如某个服务专用）先不碰。
            audience = token_audience(token)
            if audience and "account" not in audience:
                continue
            if exp > best_exp:
                best, best_exp = token, exp
        if best:
            break                      # 主进程里拿到了就不必再扫子进程
    if best:
        _CACHE.update({"token": best, "exp": best_exp})
        _save_cached_token(best, best_exp)
        return best

    # 客户端没开（或刚启动还没把登录态装进内存）：用上次加密存下来的那份。
    token, exp = _load_cached_token()
    if token and exp > now + 60:
        _CACHE.update({"token": token, "exp": exp})
        return token
    raise DirectUnavailable(
        f"没在 {PROCESS_NAME} 的进程内存里找到可用的登录令牌，本地也没有可用的加密副本"
        "（客户端没运行过，或登录态已经过期）")


def reset_cache() -> None:
    _CACHE.update({"token": "", "exp": 0.0})


async def _read_stream(response, on_progress: Callable[[dict[str, Any]], None] | None,
                       stall_seconds: float, started: float) -> dict[str, Any]:
    """把 SSE 流读成和 SDK worker 同形状的结果。"""
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict[str, Any] | None = None
    model = ""
    chunks = 0
    content_chars = 0
    reasoning_chars = 0
    last_progress = 0.0
    last_data = time.monotonic()

    async for raw in response.aiter_lines():
        line = (raw or "").strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if parsed.get("error"):
            error = parsed["error"] if isinstance(parsed["error"], dict) else {"message": parsed["error"]}
            raise RuntimeError(f"{error.get('code') or ''}：{error.get('message') or '网关返回错误'}")
        model = parsed.get("model") or model
        if parsed.get("usage"):
            usage = parsed["usage"]
        choices = parsed.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            content.append(piece)
            content_chars += len(piece)
        thought = delta.get("reasoning_content")
        if thought:
            reasoning.append(thought)
            reasoning_chars += len(thought)
        chunks += 1
        now = time.monotonic()
        if now - last_data > stall_seconds:
            raise TimeoutError(
                f"网关连续 {int(stall_seconds)} 秒没有吐出任何新内容，判定为卡住并已中止")
        last_data = now
        if on_progress and now - last_progress >= 1.5:
            last_progress = now
            on_progress({"started": started, "contentChars": content_chars,
                         "reasoningChars": reasoning_chars, "chunks": chunks})

    text = "".join(content)
    if not text:
        raise RuntimeError("网关返回了空内容（只有思考过程，没有最终 JSON）")
    return {"ok": True, "content": text, "reasoning": "".join(reasoning), "usage": usage,
            "model": model, "elapsedMs": int((time.monotonic() - started) * 1000),
            "progress": {"contentChars": content_chars, "reasoningChars": reasoning_chars}}


async def call_gateway(messages: list[dict[str, Any]], model: str, timeout_seconds: float = 300,
                       temperature: float = 0.2,
                       on_progress: Callable[[dict[str, Any]], None] | None = None,
                       stall_seconds: float | None = None,
                       json_mode: bool = True) -> dict[str, Any]:
    """直连 WorkBuddy 网关做一次流式对话。

    401 会重扫一次令牌（客户端刚轮换过）；400 且提到 response_format 时去掉它重试。
    """
    import httpx

    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True,
                               "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    timeout = httpx.Timeout(connect=20.0, read=float(timeout_seconds),
                            write=60.0, pool=20.0)
    stall = float(stall_seconds or max(120.0, timeout_seconds * 0.4))

    for attempt in (1, 2, 3):
        token = client_token(force=attempt == 2)
        started = time.monotonic()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "Accept": "text/event-stream"}
        try:
            # 网关在国内可直连，别让 HTTP_PROXY 环境变量把请求劫到只放行白名单的
            # 本地端口上去（那会表现成"登录态失效"或长时间无响应）。
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                         trust_env=False) as client:
                async with client.stream("POST", GATEWAY_URL, json=payload, headers=headers) as response:
                    if response.status_code == 401:
                        await response.aread()
                        reset_cache()
                        if attempt == 1:
                            continue
                        raise RuntimeError("WorkBuddy 登录态已失效：客户端里重新登录一次即可")
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        if (response.status_code == 400 and "response_format" in body
                                and "response_format" in payload and attempt == 1):
                            payload.pop("response_format", None)
                            continue
                        raise RuntimeError(f"网关返回 {response.status_code}：{body[:300]}")
                    return await _read_stream(response, on_progress, stall, started)
        except (httpx.TimeoutException,) as exc:
            raise TimeoutError(
                f"WorkBuddy 网关 {int(timeout_seconds)} 秒内没有读完这次请求") from exc
    raise RuntimeError("WorkBuddy 网关调用失败（重试已用尽）")


def _catalog_candidates() -> list[Path]:
    """客户端把产品配置（含模型目录）缓存在自己的 cache 下。

    雷达是独立进程，拿不到 ``WORKBUDDY_USER_DATA_DIR``，只能按几个常见位置找。
    找不到就返回空——模型目录只是设置页的辅助信息，不该拖累精读本身。
    """
    from pathlib import Path as _Path

    roots: list[_Path] = []
    for key in ("WORKBUDDY_USER_DATA_DIR", "WORKBUDDY_CONFIG_DIR"):
        value = os.environ.get(key)
        if value:
            roots.append(_Path(value))
    for key in ("APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(key)
        if value:
            roots.append(_Path(value) / "WorkBuddy")
            roots.append(_Path(value) / "workbuddy_data")
    home = _Path.home()
    roots.extend([home / ".workbuddy", home / ".codebuddy"])
    for drive in ("C", "D", "E"):
        roots.append(_Path(f"{drive}:/program/WorkBuddyData/workbuddy_data"))
    out: list[_Path] = []
    for root in roots:
        for folder in (root / "cache", root):
            try:
                out.extend(sorted(folder.glob("acc-product-config-v3-*.json")))
            except OSError:
                continue
    return out


def client_model_catalog() -> list[dict[str, Any]]:
    """读客户端缓存里的模型目录（id/name/credits/supportsImages）。"""
    import json as _json

    best: list[dict[str, Any]] = []
    stamp = -1.0
    for path in _catalog_candidates():
        try:
            mtime = path.stat().st_mtime
            if mtime <= stamp:
                continue
            data = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        models = data.get("models") or []
        if not isinstance(models, list) or not models:
            continue
        picked = []
        for item in models:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            picked.append({"id": item["id"], "name": item.get("name") or item["id"],
                           "credits": item.get("credits"),
                           "supportsImages": item.get("supportsImages")})
        if picked:
            best, stamp = picked, mtime
    return best


_PRICE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")
# 目录里混着视频、图像、补全、向量这些非对话模型，价格再低也不能拿来精读。
_NON_CHAT_MARKERS = ("video", "image", "embed", "rerank", "tts", "asr", "completion")
# fast/balanced/deep 是客户端的档位别名，不是网关认的模型 ID。
_ALIAS_IDS = {"fast-model", "balanced-model", "deep-model"}

_MODEL_CACHE: dict[str, Any] = {"at": 0.0, "rows": []}
MODEL_CACHE_TTL = 120.0


def model_price(entry: dict[str, Any]) -> float | None:
    """把目录里的 `x0.15` 读成 0.15；没标价返回 None。

    没标价不等于免费——那几个是补全/试验模型，网关照样可能计费，只是目录
    没写。拿它当"最便宜"会出事，所以一律排除。
    """
    raw = str(entry.get("credits") or "").strip()
    if not raw:
        return None
    match = _PRICE_RE.search(raw)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def is_chat_model(entry: dict[str, Any]) -> bool:
    model_id = str(entry.get("id") or "").strip().lower()
    if not model_id or model_id in _ALIAS_IDS:
        return False
    if any(marker in model_id for marker in _NON_CHAT_MARKERS):
        return False
    return model_price(entry) is not None


def ranked_chat_models(force: bool = False) -> list[dict[str, Any]]:
    """按单价从低到高排好的对话模型，免费的排最前。

    价格来自客户端自己缓存的模型目录（`acc-product-config-v3-*.json`），
    不是硬编码；官方调价或新增模型，这里跟着变，不用改代码。
    """
    now = time.monotonic()
    if not force and _MODEL_CACHE["rows"] and now - float(_MODEL_CACHE["at"]) < MODEL_CACHE_TTL:
        return [dict(row) for row in _MODEL_CACHE["rows"]]
    rows: list[dict[str, Any]] = []
    for entry in client_model_catalog():
        if not is_chat_model(entry):
            continue
        rows.append({"id": entry["id"], "name": entry.get("name") or entry["id"],
                     "price": model_price(entry)})
    rows.sort(key=lambda row: (row["price"], row["id"]))
    _MODEL_CACHE.update({"at": now, "rows": rows})
    return [dict(row) for row in rows]


def cheapest_model() -> dict[str, Any] | None:
    """当前单价最低的对话模型；目录读不到时返回 None，调用方回退原设置。"""
    try:
        rows = ranked_chat_models()
    except Exception:                          # noqa: BLE001 - 目录只是优化项
        return None
    return rows[0] if rows else None


def fallback_models(primary: str, limit: int = 2) -> list[str]:
    """主模型之外，从便宜到贵再给几个备选，用于失败后自动升级。"""
    try:
        rows = ranked_chat_models()
    except Exception:                          # noqa: BLE001
        return []
    return [row["id"] for row in rows if row["id"] != primary][:max(0, limit)]


async def probe(model: str, timeout_seconds: float = 60) -> dict[str, Any]:
    """发一个最小请求，确认网关和登录态都能用。"""
    started = time.monotonic()
    result = await call_gateway(
        [{"role": "system", "content": "只输出JSON。"},
         {"role": "user", "content": '只输出 {"ok":true}'}],
        model, timeout_seconds=timeout_seconds, temperature=0, json_mode=False,
        stall_seconds=min(45.0, timeout_seconds))
    return {"model": result.get("model") or model, "content": (result.get("content") or "")[:80],
            "seconds": round(time.monotonic() - started, 2), "usage": result.get("usage")}
