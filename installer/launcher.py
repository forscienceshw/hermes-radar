"""Radar.exe —— 主程序的图形入口。

它不自己跑服务，只负责三件事：把 supervisor 拉起来、把浏览器打开、把服务干净地停掉。
这样依赖依然住在安装目录的 .venv 里，升级代码不用重新打包这个 exe。

用法：
    Radar.exe            启动并打开界面（已启动就只把窗口叫出来），开成软件窗口
                         还是浏览器标签页由设置里的「打开方式」决定
    Radar.exe start      只启动服务，不打开界面
    Radar.exe stop       停止服务
    Radar.exe restart    重启
    Radar.exe status     看状态
    Radar.exe open       只打开界面（按设置里的打开方式）
    Radar.exe uninstall  卸载（删快捷方式、自启动、注册表项）
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

APP_NAME = "Hermes 科研雷达"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "HermesResearchRadar"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\HermesResearchRadar"
STILL_ACTIVE = 259
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = app_root()
DATA = ROOT / "data"
STATE = DATA / "supervisor.json"
STOP = DATA / "STOP_RADAR"
CONFIG = ROOT / "config" / "settings.json"


# --------------------------------------------------------------------------- #
# 小工具：不依赖 psutil 也能判断进程是否还活着
# --------------------------------------------------------------------------- #
def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _supervisor_pid() -> int:
    try:
        return int(json.loads(STATE.read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError):
        return 0


def _port() -> int:
    try:
        return int(json.loads(CONFIG.read_text(encoding="utf-8")).get("port") or 8766)
    except (OSError, ValueError):
        return 8766


def _url() -> str:
    return f"http://127.0.0.1:{_port()}"


def _pythonw() -> Path:
    for name in (".venv/Scripts/pythonw.exe", ".venv/Scripts/python.exe"):
        candidate = ROOT / name
        if candidate.is_file():
            return candidate
    found = Path(sys.executable).with_name("pythonw.exe") if sys.executable else None
    return found if found and found.is_file() else Path(sys.executable)


class _ConsoleStream:
    """把 print 的内容按 UTF-16 直接写进控制台。

    GUI 版 exe 没有 std 句柄，只能自己开 CONOUT$；而代码页（936 / 65001）随宿主
    变来变去，用文本模式写必然有一边是乱码。WriteConsoleW 不吃代码页，谁看都对。
    """

    def __init__(self) -> None:
        self._kernel32 = ctypes.windll.kernel32
        self._handle = self._kernel32.CreateFileW(
            "CONOUT$", 0x40000000 | 0x80000000, 0x00000003, None, 3, 0, None)  # GEN_WRITE|GEN_READ, SHARE, OPEN_EXISTING
        self.encoding = "utf-16-le"

    def write(self, text: str) -> int:
        if not text:
            return 0
        if self._handle in (0, -1, None):
            return len(text)
        buffer = ctypes.create_unicode_buffer(text)
        written = ctypes.c_ulong(0)
        self._kernel32.WriteConsoleW(self._handle, buffer, len(text), ctypes.byref(written), None)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def _attach_console() -> bool:
    """从命令行调用时把父窗口的输出接上，否则窗口版 exe 一句话都看不到。

    三种宿主，三种接法：
    * 管道（bash、编辑器、任务管理器转存）：PyInstaller 会按系统代码页（936）写，
      对面按 UTF-8 解就是乱码——这里改成 UTF-8；
    * 真控制台：交给 WriteConsoleW，跟代码页无关；
    * 双击：两个都没有，`_say` 会退回弹窗。
    """
    if sys.stdout is not None:
        try:
            if not sys.stdout.isatty():
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                        # noqa: BLE001
            pass
        return True
    try:
        if not ctypes.windll.kernel32.AttachConsole(-1):   # ATTACH_PARENT_PROCESS
            return False
        stream = _ConsoleStream()
        sys.stdout = stream
        sys.stderr = stream
        return True
    except Exception:
        return False


def _say(text: str) -> None:
    """有控制台就打印，没有（双击运行）就弹窗。"""
    if sys.stdout is not None:
        try:
            print(text)
            return
        except Exception:
            pass
    if _attach_console():
        try:
            print(text)
            return
        except Exception:
            pass
    message(text)


def message(text: str, error: bool = False) -> None:
    try:
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 启停
# --------------------------------------------------------------------------- #
def _wait_port(timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", _port()), 1.0):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def _shell():
    """拿到网页/托盘共用的偏好与命令通道；坏掉就返回 None，不阻塞启动。"""
    try:
        sys.path.insert(0, str(ROOT))
        from app.services import shell as module
        return module
    except Exception:                                # noqa: BLE001
        return None


def _ask_tray_open() -> bool:
    """托盘已经在跑就只让它开个窗口——不必再起一个 pythonw 去抢单实例。"""
    shell = _shell()
    if not shell:
        return False
    try:
        if not shell.alive():
            return False
        shell.send("open")
        return True
    except Exception:                                # noqa: BLE001
        return False


def start(open_browser: bool = True) -> int:
    # 从 2026-11-22 起，桌面启动交给托盘外壳：它开窗口、管关窗驻留、负责彻底
    # 退出。窗口开成「软件窗口」还是「浏览器标签页」，由设置里的打开方式决定
    # （data/shell.json 的 window_mode），这里不再替用户选。
    tray = ROOT / "radar_tray.pyw"
    if tray.is_file():
        if open_browser and _ask_tray_open():
            return 0
        args = [str(_pythonw()), str(tray)] + ([] if open_browser else ["--background"])
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        subprocess.Popen(args, cwd=str(ROOT), env=env,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_port():
            message("服务没能在 40 秒内启动。\n请打开安装目录下的 data\\logs\\supervisor.log 查看原因。", error=True)
            return 3
        if not open_browser:
            _say(f"已启动（托盘）：{_url()}")
        return 0
    if _alive(_supervisor_pid()):
        if open_browser:
            webbrowser.open(_url())
        return 0
    STOP.unlink(missing_ok=True)
    pythonw = _pythonw()
    supervisor = ROOT / "supervisor.pyw"
    if not pythonw.is_file() or not supervisor.is_file():
        message(f"安装不完整：找不到 {pythonw} 或 {supervisor}。\n请重新运行安装程序。", error=True)
        return 2
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    subprocess.Popen([str(pythonw), str(supervisor)], cwd=str(ROOT), env=env,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_port():
        message("服务没能在 40 秒内启动。\n请打开安装目录下的 data\\logs\\supervisor.log 查看原因。", error=True)
        return 3
    if open_browser:
        webbrowser.open(_url())
    else:
        _say(f"已启动：{_url()}")
    return 0


def stop() -> int:
    pid = _supervisor_pid()
    # 托盘外壳也要退，否则它会认为服务挂了、又把它拉起来。
    shell = _shell()
    if shell:
        try:
            shell.send("quit")
        except Exception:                            # noqa: BLE001
            pass
    STOP.parent.mkdir(parents=True, exist_ok=True)
    STOP.write_text("stop", encoding="utf-8")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.5)
    if _alive(pid):
        _terminate(pid)

    # supervisor 的 finally 会带走子进程；如果它被强杀，这里兜底清一次。
    for child in _children_by_cmdline():
        _terminate(child)
    STOP.unlink(missing_ok=True)
    try:
        with socket.create_connection(("127.0.0.1", _port()), 1.0):
            message("服务似乎还在运行，端口仍被占用。\n可以稍等几秒，或在任务管理器结束 pythonw.exe。", error=True)
            return 1
    except OSError:
        return 0


def _terminate(pid: int) -> None:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def _children_by_cmdline() -> list[int]:
    """只在需要兜底时才用 psutil；没装就跳过，不影响主流程。"""
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        command = " ".join(process.info.get("cmdline") or [])
        if "-m app.api" in command or "-m app.worker" in command:
            found.append(process.info["pid"])
    return found


# --------------------------------------------------------------------------- #
# 卸载
# --------------------------------------------------------------------------- #
def uninstall() -> int:
    answer = ctypes.windll.user32.MessageBoxW(
        None,
        f"要卸载 {APP_NAME} 吗？\n\n程序目录：{ROOT}\n\n"
        "会删除：桌面与开始菜单快捷方式、开机自启、安装记录。\n"
        "不会删除：data 目录里的资料库、离线页面和图片（除非你在下一步选择删除）。",
        APP_NAME, 0x04 | 0x20)  # MB_YESNO | MB_ICONQUESTION
    if answer != 6:  # IDYES
        return 0
    stop()

    import winreg
    for root, path, value in ((winreg.HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE),
                              (winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, None)):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as key:
                if value:
                    winreg.DeleteValue(key, value)
                else:
                    winreg.CloseKey(key)
                    winreg.DeleteKey(root, path)
        except OSError:
            pass
    for shortcut in _shortcut_paths():
        try:
            shortcut.unlink()
        except OSError:
            pass

    data_dir = ROOT / "data"
    size = _dir_size(data_dir)
    if size > 0:
        again = ctypes.windll.user32.MessageBoxW(
            None,
            f"资料库（data 目录）约 {size / 1024 / 1024:.0f} MB，包含你的文章、图片和数据库。\n\n"
            "要一起删除吗？选「否」会保留它，重装后可以直接继续用。",
            APP_NAME, 0x04 | 0x30)
        if again == 6:
            _rmtree(data_dir)
    message("已卸载。程序目录可以手动删除。")
    return 0


def _shortcut_paths() -> list[Path]:
    paths = []
    for env, parts in (("USERPROFILE", ("Desktop", f"{APP_NAME}.lnk")),
                       ("APPDATA", ("Microsoft", "Windows", "Start Menu", "Programs", f"{APP_NAME}.lnk"))):
        base = os.environ.get(env)
        if base:
            paths.append(Path(base).joinpath(*parts))
    return [p for p in paths if p.exists()]


def _dir_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def _rmtree(path: Path) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    if len(argv) > 1:
        # 带了参数就是命令行用法，先接控制台，输出别丢。
        _attach_console()
    action = (argv[1] if len(argv) > 1 else "launch").lower().lstrip("-")
    if action in ("launch", ""):
        return start(open_browser=True)
    if action == "start":
        return start(open_browser=False)
    if action == "stop":
        return stop()
    if action == "restart":
        stop()
        time.sleep(1)
        return start(open_browser=True)
    if action == "open":
        if _ask_tray_open():
            return 0
        return start(open_browser=True)
    if action == "status":
        running = _alive(_supervisor_pid())
        _say(f"{APP_NAME}\n地址：{_url()}\n状态：{'运行中' if running else '未运行'}\n目录：{ROOT}")
        return 0 if running else 1
    if action == "uninstall":
        return uninstall()
    if action in ("version", "-v"):
        try:
            _say((ROOT / "VERSION").read_text(encoding="utf-8").strip())
        except OSError:
            _say("unknown")
        return 0
    _say(f"未知命令：{action}\n可用：start / stop / restart / status / open / uninstall / version")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
