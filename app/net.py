import asyncio
import ipaddress
import os
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .services import purge
from .settings import get_settings

USER_AGENT = "HermesResearchRadar/0.1 (+local personal research archive)"


class UnsafeURL(ValueError):
    pass


def validate_public_url(url: str, resolve: bool = True) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeURL("仅允许有效的 HTTP/HTTPS URL")
    if parts.username or parts.password:
        raise UnsafeURL("URL 不允许包含账号或密码")
    host = parts.hostname.rstrip(".")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise UnsafeURL("禁止访问本机地址")
    if not resolve:
        # 走代理时不解析域名：墙内 DNS 会把 wikipedia/google 解析到 0.0.0.0 或
        # 假地址，本地解析一失败就把请求掐死了，等于代理白配。代理侧会自己解析。
        if not host or host.count(".") == 0:
            raise UnsafeURL(f"域名无效：{host}")
        return
    try:
        addresses = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL(f"域名解析失败：{host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise UnsafeURL(f"禁止访问非公网地址：{ip}")


# ---------------------------------------------------------------- 代理自动发现 --
#
# 需要代理的只有"直连必失败"的那几个站点。名单必须窄：把 arXiv / Europe PMC
# 这些国内直连可用的库也塞进来，一旦某台机器代理挂了、或者端口猜错了，整条
# 检索链就跟着废掉。这里只放无论如何都得翻出去才能通的域名。
PROXY_ONLY_HOSTS = (
    "google.com", "google.com.hk", "googleapis.com", "gstatic.com",
    "duckduckgo.com", "wikipedia.org", "wikimedia.org", "wiktionary.org",
    "youtube.com", "ytimg.com", "twitter.com", "x.com",
)

# 别人电脑上装的不一定是 Clash：Clash 7890/7897、v2rayN 10809/10808、
# 老版本 1080、Surge 6152、Fiddler 8888 都见过。挨个试一遍比让用户去查端口现实。
PROXY_PORTS = (7897, 7890, 7891, 10809, 10808, 1080, 6152, 8889, 8118, 8888)
PROXY_TTL = 180.0

_proxy_state: dict[str, Any] = {"at": 0.0, "proxy": "", "overseas": False}
_proxy_lock = threading.Lock()


def needs_proxy(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in PROXY_ONLY_HOSTS)


def _configured_proxy() -> str:
    try:
        return str((get_settings() or {}).get("http_proxy") or "").strip()
    except Exception:                                # noqa: BLE001 - 设置读不到就当没配
        return ""


def _system_proxy() -> str:
    """Windows「系统代理」开关的值。Clash 打开系统代理后这里就有，用户不用再填。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            if not enabled:
                return ""
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except Exception:                                # noqa: BLE001
        return ""
    if not server:
        return ""
    if "=" in server:                                # http=127.0.0.1:7897;https=127.0.0.1:7897
        parts = dict(item.split("=", 1) for item in server.split(";") if "=" in item)
        server = parts.get("https") or parts.get("http") or ""
    if not server:
        return ""
    return server if "://" in server else "http://" + server


def _env_proxy() -> str:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value if "://" in value else "http://" + value
    return ""


def _candidates() -> list[tuple[str, str]]:
    """按可信度排序的候选代理，附一个"从哪来的"标签给设置页显示。"""
    items: list[tuple[str, str]] = []
    for value, source in ((_configured_proxy(), "设置"),
                          (_system_proxy(), "系统代理"),
                          (_env_proxy(), "环境变量")):
        if value and all(value != known for known, _ in items):
            items.append((value, source))
    for port in PROXY_PORTS:
        value = f"http://127.0.0.1:{port}"
        if all(value != known for known, _ in items):
            items.append((value, "本机端口"))
    return items


def _probe_proxy(proxy: str) -> bool:
    """走这个代理真连一次维基百科，能完成 TLS 握手才算可用。

    只测端口有没有人听、或者只看 CONNECT 有没有回 200 都不够：见过一个本地
    代理对所有 CONNECT 都痛快递回 200，但只放行白名单里的域名（沙箱工具常这样），
    流量一到它那儿就断。探测目标必须是我们真正要访问的站点，不能拿一个
    "恰好被放行"的域名去测，否则死代理也会被判成活的。
    """
    if proxy.startswith("socks"):
        return True                                  # socks 不走 CONNECT，信任用户配置
    parsed = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        return False
    target = "zh.wikipedia.org"
    try:
        raw = socket.create_connection((host, port), timeout=1.2)
    except OSError:
        return False
    try:
        raw.settimeout(2.0)
        raw.sendall(f"CONNECT {target}:443 HTTP/1.1\r\nHost: {target}:443\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 4096:
            chunk = raw.recv(1024)
            if not chunk:
                return False
            head += chunk
        if b" 200" not in head.split(b"\r\n", 1)[0]:
            return False
        # TLS 证书校验通过就说明流量真的到了维基（假代理伪造不出这张证书）。
        # 不检查随后的状态码：维基对不带 UA 的 HEAD 直接回 403，那是它自己的事，
        # 与代理通不通无关。
        context = ssl.create_default_context()
        with context.wrap_socket(raw, server_hostname=target):
            return True
    except (OSError, ssl.SSLError, ValueError):
        return False
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _direct_overseas() -> bool:
    """本机不带代理能不能直接连出去（人在国外、或公司有透明网关）。"""
    try:
        context = ssl.create_default_context()
        with socket.create_connection(("zh.wikipedia.org", 443), timeout=1.5) as raw:
            with context.wrap_socket(raw, server_hostname="zh.wikipedia.org"):
                return True
    except Exception:                                # noqa: BLE001
        return False


def resolve_overseas(force: bool = False) -> dict[str, Any]:
    """探测本机能不能访问墙外站点，结果缓存 3 分钟。

    返回 {"proxy": 可用代理或空串, "overseas": 能否访问 Google/维基这类站点}。
    **没有任何代理的机器也会返回 overseas=False 而不是报错**——上层据此少挂两个
    海外源，其余源照常工作，这就是"别人电脑也能用"的实现方式。
    """
    now = time.monotonic()
    with _proxy_lock:
        if not force and now - float(_proxy_state["at"]) < PROXY_TTL:
            return dict(_proxy_state)
        candidates = _candidates()
        # 并行试：串行的话，一个"假装好心"的死代理就能吃掉好几秒。全部候选
        # 同时发，总耗时等于最慢的那一个，然后按优先级顺序取第一个通的。
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
            flags = list(pool.map(_probe_proxy, [url for url, _ in candidates]))
        proxy, source = next(((url, src) for (url, src), ok in zip(candidates, flags) if ok),
                             ("", ""))
        # 找到代理就不用再测直连：短路掉那次 1.5 秒的 TLS 尝试。
        state = {"at": now, "proxy": proxy, "source": source,
                 "overseas": bool(proxy) or _direct_overseas()}
        _proxy_state.update(state)
        return dict(state)


def proxy_snapshot() -> dict[str, Any]:
    """给设置页/诊断用的只读快照，不触发探测。"""
    return {"proxy": _proxy_state["proxy"], "source": _proxy_state.get("source", ""),
            "overseas": bool(_proxy_state["overseas"]), "probed": bool(_proxy_state["at"])}


@dataclass
class FetchResponse:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json
        return json.loads(self.content.decode("utf-8-sig"))


def _client_options(url: str = "") -> dict[str, Any]:
    """给这一个请求决定要不要挂代理。

    只有 PROXY_ONLY_HOSTS 里的域名才走代理，而且必须是 resolve_overseas() 已经
    探到可用代理。这里只读缓存、不做网络探测——它跟 request() 一样跑在事件循环
    里，在这里 connect 一次就把整次搜索卡住了。
    """
    if not url or not needs_proxy(url):
        return {}
    proxy = str(_proxy_state.get("proxy") or "")
    return {"proxy": proxy} if proxy else {}


async def request(url: str, method: str = "GET", max_bytes: int = 25_000_000,
                  headers: dict[str, str] | None = None, json_body: Any = None,
                  form_body: dict[str, Any] | None = None, timeout: float | None = None,
                  max_redirects: int = 5) -> FetchResponse:
    current = url.strip()
    current_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    original_host = urlsplit(current).hostname
    timeout = timeout or float(get_settings()["request_timeout_seconds"])
    options = _client_options(current)
    # trust_env=False 不是可选项。httpx 默认会读 HTTP_PROXY/HTTPS_PROXY，而不少
    # 桌面环境（含 WorkBuddy 自身沙箱）会把这些变量指向一个只放行白名单的本地端口：
    # 我们明明决定直连 360 / Europe PMC，流量却被它劫走，每个源都超时到十几秒，
    # 表现就是「所有源都挂了」。代理只由 _client_options 按域名决定，别让环境接管。
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout,
                                 trust_env=False, **options) as client:
        for _ in range(max_redirects + 1):
            await asyncio.to_thread(validate_public_url, current, not options)
            async with client.stream(method, current, headers=current_headers, json=json_body, data=form_body) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise httpx.HTTPError("重定向缺少 Location")
                    next_url = urljoin(current, location)
                    if urlsplit(next_url).hostname != original_host:
                        current_headers.pop("Authorization", None)
                        current_headers.pop("Cookie", None)
                    if response.status_code == 303:
                        method = "GET"
                        json_body = None
                        form_body = None
                    current = next_url
                    continue
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    raise ValueError(f"响应超过大小限制（{max_bytes} 字节）")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise ValueError(f"响应超过大小限制（{max_bytes} 字节）")
                return FetchResponse(str(response.url), response.status_code,
                                     {k.lower(): v for k, v in response.headers.items()}, bytes(chunks))
    raise httpx.TooManyRedirects("重定向次数过多")


async def download_file(url: str, dest, max_bytes: int = 100_000_000,
                        headers: dict[str, str] | None = None, attempts: int = 5,
                        read_timeout: float = 90.0) -> int:
    """Stream a large file to disk with HTTP Range resume.

    Publisher PDFs regularly drop the TCP connection halfway (Nature closed a
    28MB transfer at 5.9MB). Keeping the partial file and asking for
    ``bytes=<size>-`` turns that into a resumable download instead of a failure.
    """
    from pathlib import Path

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(attempts):
        size = dest.stat().st_size if dest.exists() else 0
        if size > max_bytes:
            raise ValueError(f"文件超过大小限制（{max_bytes} 字节）")
        current = url.strip()
        current_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        if size:
            current_headers["Range"] = f"bytes={size}-"
        timeout = httpx.Timeout(connect=30.0, read=read_timeout, write=60.0, pool=30.0)
        try:
            options = _client_options(current)
            async with httpx.AsyncClient(follow_redirects=False, timeout=timeout,
                                         trust_env=False, **options) as client:
                for _ in range(6):
                    await asyncio.to_thread(validate_public_url, current, not options)
                    async with client.stream("GET", current, headers=current_headers) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise httpx.HTTPError("重定向缺少 Location")
                            current = urljoin(current, location)
                            continue
                        if response.status_code == 416 and size:
                            return size
                        response.raise_for_status()
                        total = None
                        content_range = response.headers.get("content-range") or ""
                        if "/" in content_range:
                            try:
                                total = int(content_range.rsplit("/", 1)[1])
                            except ValueError:
                                total = None
                        if total and total > max_bytes:
                            raise ValueError(f"文件超过大小限制（{max_bytes} 字节）")
                        mode = "ab" if size and response.status_code == 206 else "wb"
                        if mode == "wb":
                            size = 0
                        with open(dest, mode) as handle:
                            async for chunk in response.aiter_bytes():
                                if not chunk:
                                    continue
                                handle.write(chunk)
                                size += len(chunk)
                                if size > max_bytes:
                                    raise ValueError(f"文件超过大小限制（{max_bytes} 字节）")
                        if size == 0:
                            raise ValueError("服务器返回空文件")
                        with dest.open("rb") as probe:
                            head = probe.read(5)
                        if head != b"%PDF-" and dest.suffix.lower() == ".part":
                            purge.unlink(dest)
                            raise ValueError("链接未返回 PDF 文件")
                        if total and size < total:
                            raise httpx.ReadError(f"连接提前结束：{size}/{total} 字节")
                        return size
        except Exception as exc:  # noqa: BLE001 - every failure is retried with resume
            last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(min(12, 2 ** attempt))
    raise RuntimeError(f"下载失败（已重试 {attempts} 次）：{last_error}")


async def get_json(url: str, headers: dict[str, str] | None = None,
                   max_bytes: int = 10_000_000) -> tuple[Any, FetchResponse]:
    response = await request(url, max_bytes=max_bytes,
                             headers={"Accept": "application/json", **(headers or {})})
    return response.json(), response


async def post_json(url: str, payload: Any, headers: dict[str, str] | None = None,
                    timeout: float | None = None, max_bytes: int = 10_000_000) -> tuple[Any, FetchResponse]:
    response = await request(url, method="POST", json_body=payload, max_bytes=max_bytes,
                             headers={"Accept": "application/json", **(headers or {})}, timeout=timeout)
    return response.json(), response


async def post_form(url: str, payload: dict[str, Any], timeout: float | None = None) -> tuple[Any, FetchResponse]:
    response = await request(url, method="POST", form_body=payload, max_bytes=5_000_000,
                             headers={"Accept": "application/json"}, timeout=timeout)
    return response.json(), response
