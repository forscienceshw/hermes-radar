import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .services import purge
from .settings import get_settings

USER_AGENT = "HermesResearchRadar/0.1 (+local personal research archive)"


class UnsafeURL(ValueError):
    pass


def validate_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeURL("仅允许有效的 HTTP/HTTPS URL")
    if parts.username or parts.password:
        raise UnsafeURL("URL 不允许包含账号或密码")
    host = parts.hostname.rstrip(".")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise UnsafeURL("禁止访问本机地址")
    try:
        addresses = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL(f"域名解析失败：{host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise UnsafeURL(f"禁止访问非公网地址：{ip}")


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


def _client_options() -> dict[str, Any]:
    """把设置里的代理接进 httpx。

    有些站点（微信公众号文章页尤其明显）会按 IP 段上风控，换出口是唯一有效的
    解法，所以给用户一个能填代理的地方，而不是让他去改系统代理。
    支持 http://host:port / socks5://user:pass@host:port。
    """
    try:
        proxy = str((get_settings() or {}).get("http_proxy") or "").strip()
    except Exception:                                # noqa: BLE001 - 设置读不到就别用代理
        return {}
    return {"proxy": proxy} if proxy else {}


async def request(url: str, method: str = "GET", max_bytes: int = 25_000_000,
                  headers: dict[str, str] | None = None, json_body: Any = None,
                  form_body: dict[str, Any] | None = None, timeout: float | None = None,
                  max_redirects: int = 5) -> FetchResponse:
    current = url.strip()
    current_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    original_host = urlsplit(current).hostname
    timeout = timeout or float(get_settings()["request_timeout_seconds"])
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout,
                                 **_client_options()) as client:
        for _ in range(max_redirects + 1):
            await asyncio.to_thread(validate_public_url, current)
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
            async with httpx.AsyncClient(follow_redirects=False, timeout=timeout,
                                         **_client_options()) as client:
                for _ in range(6):
                    await asyncio.to_thread(validate_public_url, current)
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
