"""Non-destructive inspection of the WeChat install on this machine.

The settings wizard needs to answer one question before the user clicks anything:
*what kind of WeChat is here, and can we already read it?*  Every function in
this module is read-only — it opens no database, writes no secret and never
touches the WeChat process beyond listing its name, path and version.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..paths import BASE_DIR, DATA_DIR
from ..settings import get_secret
from ..utils import json_loads

ACCOUNT_FILE = DATA_DIR / "wechat-self" / "account.json"
VENDOR_VERSIONS = (BASE_DIR / "analysis" / "wechatmsg_full" / "app" / "decrypt" / "version_list.json")


def _file_version(path: str) -> str:
    try:
        from win32api import GetFileVersionInfo, HIWORD, LOWORD
        info = GetFileVersionInfo(path, "\\")
        return f"{HIWORD(info['FileVersionMS'])}.{LOWORD(info['FileVersionMS'])}.{HIWORD(info['FileVersionLS'])}.{LOWORD(info['FileVersionLS'])}"
    except Exception:
        return ""


def _processes() -> list[dict[str, Any]]:
    """Return WeChat/Weixin processes with the four fields the wizard shows."""
    try:
        import psutil
    except ImportError:
        return []
    found: list[dict[str, Any]] = []
    for process in psutil.process_iter(["name", "exe", "pid"]):
        name = (process.info.get("name") or "").lower()
        if name not in ("wechat.exe", "weixin.exe"):
            continue
        exe = process.info.get("exe") or ""
        found.append({
            "name": process.info.get("name"),
            "pid": process.info.get("pid"),
            "exe": exe,
            "version": _file_version(exe) if exe else "",
            "family": "3.x" if name == "wechat.exe" else "4.x",
        })
    return found


def _supported_versions() -> set[str]:
    try:
        return set(json.loads(VENDOR_VERSIONS.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _registry_save_path(key_path: str, value: str) -> str:
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            data, _ = winreg.QueryValueEx(key, value)
            return os.path.expandvars(str(data or "")).strip()
    except OSError:
        return ""


def _candidate_roots(family: str) -> list[Path]:
    """Known data locations only — never scan whole drives."""
    home = Path.home()
    if family == "4.x":
        base = _registry_save_path(r"Software\Tencent\Weixin", "FileSavePath")
        roots = [Path(base)] if base else []
        roots += [home / "Documents" / "xwechat_files", home / "xwechat_files"]
    else:
        base = _registry_save_path(r"Software\Tencent\WeChat", "FileSavePath")
        roots = [Path(base)] if base else []
        roots += [home / "Documents" / "WeChat Files"]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        marker = str(root).casefold()
        if marker and marker not in seen and root.is_dir():
            seen.add(marker)
            unique.append(root)
    return unique


def _accounts_in(root: Path, family: str) -> list[dict[str, str]]:
    finds: list[dict[str, str]] = []
    try:
        children = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return finds
    for child in children:
        if not child.name.startswith("wxid_"):
            continue
        db_root = child / "db_storage" if family == "4.x" else child
        finds.append({"wxid": child.name, "db_root": str(db_root), "exists": db_root.is_dir()})
    return finds


def _saved_account() -> dict[str, Any]:
    if not ACCOUNT_FILE.is_file():
        return {}
    return json_loads(ACCOUNT_FILE.read_text(encoding="utf-8"), {}) or {}


def _source_state() -> dict[str, Any]:
    """Which WeChat collectors are switched on, so the wizard can point at them."""
    try:
        from ..db import Database
        from ..paths import DB_PATH
        rows = Database(DB_PATH).query("SELECT type,enabled,name FROM sources WHERE type LIKE 'wechat%'")
    except Exception:
        rows = []
    return {
        "local": any(r["type"] == "wechat_local" and r["enabled"] for r in rows),
        "public": any(r["type"] == "wechat_public_local" and r["enabled"] for r in rows),
        "sources": [{"type": r["type"], "enabled": bool(r["enabled"]), "name": r["name"]} for r in rows],
    }


def status() -> dict[str, Any]:
    """Everything the wizard's WeChat step needs, in one cheap call."""
    processes = _processes()
    supported = _supported_versions()
    saved = _saved_account()
    key = get_secret("wechat_db_key")

    running = next((p for p in processes if p["family"] == "3.x"), None)
    running4 = next((p for p in processes if p["family"] == "4.x"), None)
    active = running or running4

    family = active["family"] if active else ("3.x" if saved.get("wechat_version", "").startswith("3.") else "")
    version = (active or {}).get("version") or saved.get("wechat_version", "")

    roots = _candidate_roots(family or "3.x")
    accounts = [account for root in roots for account in _accounts_in(root, family or "3.x")]

    four = {}
    if family == "4.x":
        try:
            from . import wechat4
            four = wechat4.quick_status()
        except Exception as exc:            # 适配器出问题也不能让设置页打不开
            four = {"error": f"{type(exc).__name__}: {exc}"}
        if four.get("databases"):
            state = "4x-ready"
            hint = ("检测到微信 4.x，并找到了 xwechat_files 数据库。"
                    "可以点「尝试读取 4.x」执行一次只读解锁验证。")
        else:
            state = "unsupported-4x"
            hint = ("检测到微信 4.x，但没有找到 xwechat_files 数据目录。"
                    "请先用 4.x 登录一次，再回到这里检测。")
    elif active:
        state = "ready" if version in supported else "version-unknown"
        hint = "" if version in supported else f"微信 {version} 不在已验证版本表里，读取可能失败，但可以试。"
    elif saved and key:
        state = "offline"
        hint = "微信没在运行；已存过账号和密钥，可以读上次的离线目录。"
    elif not accounts:
        state = "missing"
        hint = "没找到微信数据目录。确认这台电脑登录过微信电脑版。"
    else:
        state = "need-login"
        hint = "找到微信数据目录，但需要微信正在运行并已登录，才能取到数据库密钥。"

    return {
        "state": state,
        "four": four,
        "hint": hint,
        "family": family,
        "version": version,
        "supported": bool(version) and version in supported,
        "running": bool(active),
        "processes": processes,
        "exe": (active or {}).get("exe", ""),
        "key_saved": bool(key),
        "saved_account": {"wxid": saved.get("wxid", ""), "db_root": saved.get("db_root", "")},
        "accounts": accounts,
        "roots": [str(root) for root in roots],
        "sources": _source_state(),
        "supported_versions": len(supported),
    }
