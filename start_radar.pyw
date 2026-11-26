"""双击 "Start Radar.vbs" 时实际跑的就是这里。

放在 Python 而不是 VBS 里，是因为要检查端口、判断是否已经在跑、给中文提示，
这些用 VBScript 写会很难看。VBS 只负责把窗口藏起来然后调我。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
SUPERVISOR = ROOT / "supervisor.pyw"
STOP_FILE = ROOT / "data" / "STOP_RADAR"
STATE = ROOT / "data" / "supervisor.json"


def message(text: str, error: bool = False) -> None:
    # 无人值守排查时设 RADAR_NO_POPUP=1，消息走 stderr 而不是弹窗。
    if os.environ.get("RADAR_NO_POPUP") == "1":
        print(text, file=sys.stderr, flush=True)
        return
    flags = 0x10 if error else 0x40
    ctypes.windll.user32.MessageBoxW(None, text, "Hermes 科研雷达", flags)


def base_url() -> str:
    try:
        settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
        port = int(settings.get("port") or 8766)
    except (OSError, ValueError):
        port = 8766
    return f"http://127.0.0.1:{port}"


def supervisor_pid() -> int:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return int(data.get("pid") or 0)
    except (OSError, ValueError):
        return 0


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
    except ImportError:
        return False
    return psutil.pid_exists(pid)


def open_browser() -> None:
    # 自动化测试或远程桌面里不想弹窗口时设 RADAR_NO_BROWSER=1。
    if os.environ.get("RADAR_NO_BROWSER") == "1":
        return
    webbrowser.open(base_url())


def main() -> int:
    if not PYTHONW.is_file():
        message("还没装运行环境。\n\n请先在这个文件夹里运行：\n    python install.py", error=True)
        return 1
    if not SUPERVISOR.is_file():
        message("找不到 supervisor.pyw，这个文件夹可能不完整。", error=True)
        return 1

    # 上次点了"停止"，标记文件还留着；不清掉的话新进程一启动就会退出。
    try:
        STOP_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    if alive(supervisor_pid()):
        open_browser()
        return 0

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(ROOT))
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("TEMP", str(ROOT / ".tmp"))
    env.setdefault("TMP", str(ROOT / ".tmp"))
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    creation = 0x00000008 | 0x00000200 | 0x08000000
    subprocess.Popen([str(PYTHONW), str(SUPERVISOR)], cwd=str(ROOT), env=env,
                     creationflags=creation, close_fds=True)

    # 等它把网页端口拉起来。起来得慢通常是首次导入依赖，给足 20 秒。
    for _ in range(20):
        time.sleep(1)
        pid = supervisor_pid()
        if pid and alive(pid):
            break
    else:
        message("服务好像没起来。\n\n看看这个文件里最后几行写了什么：\n"
                f"{ROOT / 'data' / 'logs' / 'app-api-console.log'}", error=True)
        return 1
    open_browser()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pythonw 没有控制台，异常必须弹出来，否则用户只看到"没反应"
        message(f"启动失败：{type(exc).__name__}: {exc}", error=True)
        raise SystemExit(1)
