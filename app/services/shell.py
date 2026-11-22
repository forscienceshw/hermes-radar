"""桌面外壳（托盘程序）与网页之间的那点约定。

网页跑在浏览器/Edge 应用窗口里，它没法直接「把自己最小化到托盘」，也没法
「把自己连同后台服务一起彻底关掉」——这两件事归 ``radar_tray.pyw`` 管。两边
不能互相 import（一个是 .pyw 入口、一个在 app 包里），所以用两个小文件通信：

* ``data/shell.json``    用户偏好：关窗是驻留托盘还是退出、开机要不要弹窗；
* ``data/shell_cmd.json`` 网页发的一次性命令：open / quit / restart。

命令用「写文件 + 读走就删」而不是 HTTP 回调：托盘程序不开端口、不需要鉴权，
网页进程重启也不会漏掉命令。文件写入一律先写临时文件再替换，避免托盘读到
半个 JSON。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..paths import DATA_DIR

SHELL_FILE = DATA_DIR / "shell.json"
CMD_FILE = DATA_DIR / "shell_cmd.json"
# 托盘每 5 秒写一次心跳。网页靠它判断「现在有没有外壳在跑」：没有就直接开
# 浏览器窗口，有就把命令留给托盘，免得开出两个窗口。
ALIVE_FILE = DATA_DIR / "shell_alive.json"
HEARTBEAT_TTL = 20.0

# close_action：
#   tray  关掉窗口后继续在托盘里跑（默认，用户要的就是这个）
#   quit  关掉窗口就停止服务、退出托盘
DEFAULTS: dict[str, Any] = {
    "close_action": "tray",
    "open_on_start": True,
    "auto_restart": True,
}
COMMANDS = ("open", "quit", "restart")


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load() -> dict[str, Any]:
    data = {**DEFAULTS, **_read(SHELL_FILE)}
    if data.get("close_action") not in ("tray", "quit"):
        data["close_action"] = DEFAULTS["close_action"]
    for key in ("open_on_start", "auto_restart"):
        data[key] = bool(data.get(key))
    return data


def save(**patch: Any) -> dict[str, Any]:
    data = {**load(), **{k: v for k, v in patch.items() if k in DEFAULTS}}
    if data.get("close_action") not in ("tray", "quit"):
        data["close_action"] = DEFAULTS["close_action"]
    _write(SHELL_FILE, data)
    return data


def send(command: str) -> dict[str, Any]:
    """给托盘留一条命令。托盘没在跑时命令文件会一直躺着，下次启动读走即弃。"""
    name = (command or "").strip().lower()
    if name not in COMMANDS:
        raise ValueError(f"未知命令：{command}")
    payload = {"cmd": name, "at": time.time()}
    ok = _write(CMD_FILE, payload)
    return {"ok": ok, "cmd": name, "at": payload["at"], "tray_running": alive()}


def take() -> str:
    """托盘侧调用：读走命令并删除。过期超过 2 分钟的命令直接丢掉。"""
    data = _read(CMD_FILE)
    try:
        CMD_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    name = str(data.get("cmd") or "").strip().lower()
    if name not in COMMANDS:
        return ""
    try:
        age = time.time() - float(data.get("at") or 0)
    except (TypeError, ValueError):
        return ""
    return name if age <= 120 else ""


def heartbeat(pid: int | None = None) -> None:
    _write(ALIVE_FILE, {"pid": int(pid or os.getpid()), "at": time.time()})


def alive(ttl: float = HEARTBEAT_TTL) -> bool:
    data = _read(ALIVE_FILE)
    try:
        return time.time() - float(data.get("at") or 0) <= ttl
    except (TypeError, ValueError):
        return False


def status() -> dict[str, Any]:
    """给设置页看的：偏好 + 命令通道是否可用。"""
    data = load()
    return {
        **data,
        "tray_running": alive(),
        "shell_file": str(SHELL_FILE),
        "command_file": str(CMD_FILE),
        "supported": os.name == "nt",
    }
