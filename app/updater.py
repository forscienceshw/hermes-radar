"""自动更新：查新版本、下安装包、静默覆盖自己。

元数据放在仓库根的 version.json，发版时由 tools/release.py 写进去。读取顺序是
"最快失败的那个先排后面"：

    GitHub REST API   /releases/latest   最权威、没有 CDN 缓存，但国内偶尔不通
    jsDelivr CDN      @main/version.json 国内直连通常没问题，缺点是有几小时缓存
    raw.githubusercontent.com            GitHub 原站，常常要代理

任何一步失败都不往上抛：更新是锦上添花，断网时软件必须照常跑。检查结果落盘到
data/updates/state.json，所以重启后界面还能显示"有个新版本等着装"。

安装包只在用户点"立即更新"时才开始下载，不在后台偷偷下 120 MB。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .paths import BASE_DIR, DATA_DIR
from .services import purge
from .settings import get_settings

REPOSITORY = "forscienceshw/hermes-radar"
ASSET_NAME = "RadarSetup.exe"
USER_AGENT = "HermesResearchRadar-updater/1"

UPDATE_DIR = DATA_DIR / "updates"
STATE_PATH = UPDATE_DIR / "state.json"

# 国内访问 GitHub 的三条路。第一条拿到的信息最全（带 sha256），后两条是兜底。
VERSION_URLS = (
    f"https://cdn.jsdelivr.net/gh/{REPOSITORY}@main/version.json",
    f"https://raw.githubusercontent.com/{REPOSITORY}/main/version.json",
    f"https://raw.gitmirror.com/{REPOSITORY}/main/version.json",
)
API_LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"

_lock = threading.Lock()
_progress: dict[str, Any] = {"state": "idle", "percent": 0, "bytes": 0, "total": 0,
                             "path": "", "error": "", "started_at": 0, "finished_at": 0}


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #
def version_tuple(text: str) -> tuple[int, int, int]:
    """只取前三段数字。1.2.3-beta.1、v1.2.3、1.2 都能吃。"""
    parts: list[int] = []
    for chunk in str(text or "").strip().lstrip("vV").replace("-", ".").replace("+", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def current_version() -> str:
    try:
        text = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip().splitlines()
        return text[0].strip() if text else "0.0.0"
    except OSError:
        return "0.0.0"


def is_newer(candidate: str, reference: str) -> bool:
    return version_tuple(candidate) > version_tuple(reference)


# --------------------------------------------------------------------------- #
# 状态落盘
# --------------------------------------------------------------------------- #
def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data: dict[str, Any]) -> None:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# --------------------------------------------------------------------------- #
# 查新
# --------------------------------------------------------------------------- #
def _client(timeout: float) -> httpx.Client:
    # trust_env 让 httpx 认 HTTPS_PROXY；国内机器多半靠它才连得上 GitHub。
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


def _from_version_json(payload: dict[str, Any]) -> dict[str, Any]:
    asset = payload.get("asset") or {}
    return {
        "latest": str(payload.get("version") or "").strip(),
        "notes": str(payload.get("notes") or "").strip(),
        "notes_url": str(payload.get("notes_url") or "").strip(),
        "url": str(asset.get("url") or "").strip(),
        "size": int(asset.get("size") or 0),
        "sha256": str(asset.get("sha256") or "").strip().lower(),
        "published_at": str(payload.get("released_at") or "").strip(),
        "min_version": str(payload.get("min_version") or "").strip(),
    }


def _from_release_json(payload: dict[str, Any]) -> dict[str, Any]:
    tag = str(payload.get("tag_name") or "").strip()
    assets = payload.get("assets") or []
    chosen = next((item for item in assets
                   if str(item.get("name", "")).lower() == ASSET_NAME.lower()), None)
    return {
        "latest": tag.lstrip("vV"),
        "notes": str(payload.get("body") or "").strip()[:2000],
        "notes_url": str(payload.get("html_url") or "").strip(),
        "url": str((chosen or {}).get("browser_download_url") or "").strip(),
        "size": int((chosen or {}).get("size") or 0),
        "sha256": "",
        "published_at": str(payload.get("published_at") or "").strip(),
        "min_version": "",
    }


def check(timeout: float = 8.0) -> dict[str, Any]:
    """返回一份检查结果，永远不抛异常。

    两条线并行取：GitHub API 的 latest release（快、无缓存、但限流），和仓库里的
    version.json（带 sha256、能把安装包校验死）。谁先拿到算谁的，但如果 API 报的
    版本比 version.json 新，以 API 为准 —— jsDelivr 对分支有几小时缓存，发版后
    客户端可能还在读旧的 version.json。
    """
    current = current_version()
    settings = get_settings()
    if not settings.get("auto_update_check", True):
        return {"ok": False, "skipped": True, "current": current, "latest": current,
                "has_update": False, "notes": "", "url": "", "size": 0, "sha256": "",
                "source": "", "error": "已在设置里关闭自动检查",
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    errors: list[str] = []

    api_info: dict[str, Any] | None = None
    try:
        with _client(timeout) as client:
            response = client.get(API_LATEST)
        if response.status_code == 200:
            api_info = _from_release_json(response.json())
        else:
            errors.append(f"api.github.com HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001 - 换下一个源
        errors.append(f"api.github.com {type(exc).__name__}")

    json_info: dict[str, Any] | None = None
    json_source = ""
    for url in VERSION_URLS:
        try:
            with _client(timeout) as client:
                response = client.get(url)
            if response.status_code != 200:
                errors.append(f"{_host_of(url)} HTTP {response.status_code}")
                continue
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get("version"):
                errors.append(f"{_host_of(url)} 内容不合法")
                continue
            json_info, json_source = _from_version_json(payload), _host_of(url)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{_host_of(url)} {type(exc).__name__}")

    found: dict[str, Any] | None = None
    source = ""
    if json_info and json_info.get("latest"):
        found, source = dict(json_info), json_source
    if api_info and api_info.get("latest"):
        if found is None or is_newer(api_info["latest"], found["latest"]):
            # API 更新（多半是 CDN 还没刷新），用它；sha256 只能放弃
            found, source = dict(api_info), "api.github.com"
        elif api_info["latest"] == found["latest"]:
            found.setdefault("notes", api_info.get("notes") or "")
            found["notes"] = found.get("notes") or api_info.get("notes") or ""
            found["notes_url"] = found.get("notes_url") or api_info.get("notes_url") or ""

    checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
    if not found or not found.get("latest"):
        return {"ok": False, "current": current, "latest": "", "has_update": False,
                "notes": "", "url": "", "size": 0, "sha256": "", "source": "",
                "error": "；".join(errors) or "拿不到版本信息", "checked_at": checked_at}

    latest = found["latest"]
    minimum = found.get("min_version") or "0.0.0"
    forced = bool(minimum and is_newer(minimum, current))
    result = {
        **found,
        "ok": True,
        "current": current,
        "has_update": is_newer(latest, current),
        "forced": forced,
        "source": source,
        "error": "" if found.get("url") else "拿到了版本号，但没有安装包地址",
        "checked_at": checked_at,
    }
    _write_state({**result, "downloaded": _downloaded_path(latest) is not None})
    return result


def _host_of(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return url


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def _downloaded_path(version: str) -> Path | None:
    target = UPDATE_DIR / f"RadarSetup-{version}.exe"
    if target.is_file() and target.stat().st_size > 1024 * 1024:
        return target
    return None


def _mirrored(url: str) -> str:
    """给 GitHub 直链套一个镜像前缀。国内直连 release asset 常年几十 KB/s。"""
    mirror = str(get_settings().get("update_mirror") or "").strip()
    if not mirror or "github.com/" not in url:
        return url
    if not mirror.endswith("/"):
        mirror += "/"
    return mirror + url.split("github.com/", 1)[1]


def download_state() -> dict[str, Any]:
    with _lock:
        return dict(_progress)


def _set_progress(**kwargs: Any) -> None:
    with _lock:
        _progress.update(kwargs)


def download(url: str, version: str, expected_sha: str = "", timeout: float = 60.0,
             progress: bool = True) -> Path:
    """同步下载安装包。断点续传靠 Range，失败自动从头再来一次。"""
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATE_DIR / f"RadarSetup-{version}.exe"
    partial = target.with_suffix(".part")
    if target.is_file() and expected_sha and _sha256(target) == expected_sha:
        return target

    url = _mirrored(url)
    _set_progress(state="downloading", percent=0, bytes=0, total=0, path=str(target),
                  error="", started_at=time.time(), finished_at=0)
    try:
        _download_once(url, partial, timeout, progress)
        if expected_sha:
            actual = _sha256(partial)
            if actual != expected_sha:
                purge.unlink(partial)
                raise RuntimeError(f"安装包校验不通过（期望 {expected_sha[:12]}… 实际 {actual[:12]}…）")
        partial.replace(target)
        _set_progress(state="done", percent=100, finished_at=time.time())
        _remember_download(version, target)
        return target
    except Exception as exc:
        _set_progress(state="error", error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
        raise


def _download_once(url: str, partial: Path, timeout: float, progress: bool) -> None:
    resume = partial.stat().st_size if partial.is_file() else 0
    headers = {"User-Agent": USER_AGENT}
    if resume:
        headers["Range"] = f"bytes={resume}-"
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url) as response:
            if response.status_code == 416:  # 已经下完了
                return
            if response.status_code not in (200, 206):
                raise RuntimeError(f"下载失败：HTTP {response.status_code}")
            if response.status_code == 200:
                resume = 0
            total = int(response.headers.get("content-length") or 0) + resume
            if progress:
                _set_progress(total=total, bytes=resume)
            mode = "ab" if resume else "wb"
            done = resume
            with partial.open(mode) as handle:
                for chunk in response.iter_bytes(256 * 1024):
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        percent = int(done * 100 / total) if total else 0
                        _set_progress(bytes=done, total=total, percent=min(99, percent))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _remember_download(version: str, path: Path) -> None:
    state = _read_state()
    state.update({"downloaded": True, "downloaded_version": version, "downloaded_path": str(path)})
    _write_state(state)


# --------------------------------------------------------------------------- #
# 应用更新
# --------------------------------------------------------------------------- #
def _port() -> int:
    try:
        return int(get_settings().get("port") or 8766)
    except (TypeError, ValueError):
        return 8766


def apply(version: str, restart: bool = True) -> dict[str, Any]:
    """拉起安装包做静默覆盖，然后让当前实例自己退出。

    顺序很讲究：先把安装器拉起来（它带 DETACHED_PROCESS，不随我们退出而死），
    再写 STOP_RADAR 让 supervisor 收工。反过来会死锁 —— 安装器等文件解锁，
    而我们还在跑。
    """
    installer = _downloaded_path(version)
    if not installer:
        raise RuntimeError("安装包还没下完")
    if os.name != "nt":
        raise RuntimeError("只有 Windows 支持静默覆盖安装")

    arguments = [str(installer), "/S", "--upgrade", f"--dir={BASE_DIR}", f"--port={_port()}"]
    if restart:
        arguments.append("--launch")
    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    subprocess.Popen(arguments, cwd=str(installer.parent), close_fds=True, creationflags=flags)

    stop_file = DATA_DIR / "STOP_RADAR"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(f"update {version}", encoding="utf-8")
    return {"ok": True, "version": version, "installer": str(installer), "port": _port()}


def status() -> dict[str, Any]:
    """给界面用：把落盘的检查结果和内存里的下载进度揉在一起。"""
    state = _read_state()
    current = current_version()
    latest = state.get("latest") or ""
    # 装完之后 state.json 里还留着上一个版本，得按当前版本重新算一次。
    has_update = bool(latest) and is_newer(latest, current)
    downloaded = _downloaded_path(latest) if latest else None
    return {
        "current": current,
        "latest": latest,
        "has_update": has_update,
        "forced": bool(state.get("forced")) and has_update,
        "notes": state.get("notes") or "",
        "notes_url": state.get("notes_url") or "",
        "size": state.get("size") or 0,
        "sha256": state.get("sha256") or "",
        "url": state.get("url") or "",
        "source": state.get("source") or "",
        "checked_at": state.get("checked_at") or "",
        "error": state.get("error") or "",
        "downloaded": bool(downloaded),
        "downloaded_path": str(downloaded) if downloaded else "",
        "download": download_state(),
        "repository": f"https://github.com/{REPOSITORY}",
    }
