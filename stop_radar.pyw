"""双击 "Stop Radar.vbs" 时跑这里。写完停止标记顺带确认进程真的退了。"""

from __future__ import annotations

import ctypes
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STOP_FILE = ROOT / "data" / "STOP_RADAR"
STATE = ROOT / "data" / "supervisor.json"


def message(text: str, error: bool = False) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, "Hermes 科研雷达", 0x10 if error else 0x40)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
    except ImportError:
        return False
    return psutil.pid_exists(pid)


def main() -> int:
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text("stop", encoding="utf-8")

    try:
        pid = int(json.loads(STATE.read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError):
        pid = 0

    if not pid:
        return 0
    # supervisor 一看到标记就收工，它会先 terminate 两个子进程再自己退出。
    for _ in range(15):
        if not pid_alive(pid):
            return 0
        time.sleep(1)
    message(f"已经发出停止信号，但进程 {pid} 还在。\n\n"
            "如果它能自己退就没问题；一直不退就在任务管理器里结束它。", error=True)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        message(f"停止失败：{type(exc).__name__}: {exc}", error=True)
        raise SystemExit(1)
