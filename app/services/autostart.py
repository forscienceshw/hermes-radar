"""Start-at-login, without a scheduled task and without admin rights.

Two things are supported and reported separately:

* the modern one — a value under HKCU\\...\\CurrentVersion\\Run pointing at
  Radar.exe (or, on a source checkout, at pythonw.exe + supervisor.pyw);
* the legacy one — a shortcut in the user's Startup folder, which is what the
  first version of this program installed by hand.

`enable()` writes the first and removes the second, so a machine that upgraded
never ends up launching two copies at login.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from ..paths import BASE_DIR

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "HermesResearchRadar"
LEGACY_NAMES = ("Start Radar.vbs.lnk", "Radar.lnk")


def startup_folder() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" if appdata else Path()


def launcher_command() -> list[str]:
    """The command Windows should run at login.

    A frozen build ships Radar.exe next to the code, so that wins. A source
    checkout has no exe, and pythonw.exe + supervisor.pyw is exactly what the
    old VBS did — same result, one less layer.
    """
    exe = BASE_DIR / "Radar.exe"
    if exe.is_file():
        return [str(exe), "start"]
    local = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    pythonw = local if local.is_file() else Path(sys.executable)
    if pythonw.name.lower() != "pythonw.exe":
        candidate = pythonw.with_name("pythonw.exe")
        if candidate.is_file():
            pythonw = candidate
    return [str(pythonw), str(BASE_DIR / "supervisor.pyw")]


def _read_value() -> str:
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value or "")
    except OSError:
        return ""


def _shortcuts() -> list[str]:
    folder = startup_folder()
    if not folder or not folder.is_dir():
        return []
    return [name for name in LEGACY_NAMES if (folder / name).exists()]


def status() -> dict[str, Any]:
    value = _read_value()
    shortcuts = _shortcuts()
    command = launcher_command()
    return {
        "enabled": bool(value),
        "mode": "registry" if value else ("legacy_shortcut" if shortcuts else "off"),
        "command": value or " ".join(f'"{part}"' if " " in part else part for part in command),
        "target": command[0],
        "legacy_shortcuts": shortcuts,
        "startup_folder": str(startup_folder()),
        "supported": os.name == "nt",
    }


def enable() -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("只有 Windows 支持开机自启")
    import subprocess
    import winreg
    command = subprocess.list2cmdline(launcher_command())
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    # 旧的启动文件夹快捷方式是第一版留下的，留着会和注册表项重复拉起。
    removed = []
    folder = startup_folder()
    for name in _shortcuts():
        try:
            (folder / name).unlink()
            removed.append(name)
        except OSError:
            pass
    result = status()
    result["removed_legacy"] = removed
    return result


def disable() -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("只有 Windows 支持开机自启")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except OSError:
        pass
    removed = []
    folder = startup_folder()
    for name in _shortcuts():
        try:
            (folder / name).unlink()
            removed.append(name)
        except OSError:
            pass
    result = status()
    result["removed_legacy"] = removed
    return result
