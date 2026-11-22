"""Hermes 科研雷达 · 桌面外壳。

双击桌面图标跑的就是这个文件（``pythonw.exe radar_tray.pyw``）。它做的事：

1. 拉起后台（``supervisor.pyw`` → api + worker），等服务端口通了再开窗口；
2. 用 Edge 的「应用窗口」模式打开界面——没有地址栏和标签页，看起来就是个桌面
   程序，而且进程生命周期＝窗口生命周期，关窗能被准确感知；
3. 在右下角放一个托盘图标：双击重新打开窗口，右键菜单能重启服务、看状态、
   改「关窗是否连服务一起退出」、彻底退出。

为什么不用 pywebview：这台机器是 Python 3.14，pythonnet / WebView2 的轮子跟不上，
装不上就等于没有窗口。Edge 应用窗口零依赖，Windows 10/11 都自带。

为什么关窗后服务还在：用户要的就是这个——关掉界面不等于停掉采集。真要停，
走托盘菜单「彻底退出」或设置页的「彻底退出」，那条路会写停止标记并等
supervisor 把两个子进程收干净，不留孤儿端口。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
CONFIG = ROOT / "config" / "settings.json"
# 开发实例会把 RADAR_DATA_DIR / RADAR_CONFIG_DIR 指到别处；能读就读，读不到按
# 经典布局来。托盘只关心「服务和数据在哪个目录」，不需要连数据库。
try:
    from app.paths import CONFIG_DIR as _CONFIG_DIR, DATA_DIR as _DATA_DIR
    DATA = _DATA_DIR
    CONFIG = _CONFIG_DIR / "settings.json"
except Exception:                                    # noqa: BLE001
    pass
STATE = DATA / "supervisor.json"
STOP = DATA / "STOP_RADAR"
# Edge 的独立配置目录放 .cache，不混进 data/：里面全是浏览器缓存（首次启动
# 就有几十兆），备份、体检、迁移都不该把它当用户资料。
PROFILE = ROOT / ".cache" / "appwin"
ICON = DATA / "radar.ico"

APP_NAME = "Hermes 科研雷达"
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
STILL_ACTIVE = 259

WM_TRAY = 0x8000 + 20
WM_TICK = 0x8000 + 21
WM_WINDOW_CLOSED = 0x8000 + 22

ID_OPEN, ID_BROWSER, ID_STATUS = 1001, 1002, 1003
ID_APPWIN = 1004
ID_RESTART, ID_STOP = 1011, 1012
ID_DATA, ID_AUTOSTART, ID_OPENWIN = 1021, 1022, 1023
ID_CLOSE_ASK, ID_CLOSE_TRAY, ID_CLOSE_QUIT = 1031, 1032, 1033
ID_QUIT = 1099
# 托盘菜单的每一项文字。抽成常量是为了测试能直接核对，不用去戳 Win32。
MENU_TEXT = {
    ID_OPEN: "打开界面",
    ID_APPWIN: "    软件窗口（无地址栏）",
    ID_BROWSER: "    浏览器标签页",
    ID_RESTART: "重启服务",
    ID_STOP: "停止服务（保留托盘）",
    ID_CLOSE_ASK: "    每次问我",
    ID_CLOSE_TRAY: "    只关窗口，服务留在托盘",
    ID_CLOSE_QUIT: "    连后台服务一起退出",
    ID_AUTOSTART: "开机自动启动",
    ID_OPENWIN: "开机启动时打开界面",
    ID_DATA: "打开数据文件夹",
    ID_QUIT: "彻底退出（停止服务）",
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
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
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.is_file() else exe


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(ROOT))
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("TEMP", str(ROOT / ".tmp"))
    env.setdefault("TMP", str(ROOT / ".tmp"))
    # 采集器会把 PublicMsg.db 复制成临时快照再删掉，本机的安全删除垫片会把
    # 每次删除记进回收站，一天几千条。自己的临时文件不需要这层兜底。
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    return env


def _wait_port(timeout: float = 45.0) -> bool:
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", _port()), 1.0):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def _edge_exe() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)") or "") / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles") or "") / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA") or "") / "Microsoft/Edge/Application/msedge.exe",
    ]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
            candidates.append(Path(str(winreg.QueryValueEx(key, "")[0])))
    except OSError:
        pass
    for path in candidates:
        try:
            if str(path) and path.is_file():
                return path
        except OSError:
            continue
    return None


def _make_icon() -> Path:
    """画一个雷达图标（同心圆 + 一条扫描线）。PIL 缺了就退回系统图标。"""
    if ICON.is_file():
        return ICON
    try:
        from PIL import Image, ImageDraw
    except Exception:                                # noqa: BLE001
        return ICON
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 3, size - 3), fill=(18, 110, 103, 255))
    for radius in (12, 20, 27):
        box = (size / 2 - radius, size / 2 - radius, size / 2 + radius, size / 2 + radius)
        draw.ellipse(box, outline=(210, 240, 236, 210), width=2)
    draw.line((size / 2, size / 2, size / 2 + 20, size / 2 - 14), fill=(255, 255, 255, 235), width=3)
    draw.ellipse((size / 2 - 3, size / 2 - 3, size / 2 + 3, size / 2 + 3), fill=(255, 255, 255, 255))
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        image.save(ICON, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    except OSError:
        return ICON
    return ICON


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #
class TrayApp:
    def __init__(self, *, background: bool = False, with_service: bool = True,
                 mode: str = "", autostart: bool = False) -> None:
        self.background = background
        self.with_service = with_service
        self.forced_mode = mode if mode in ("app", "browser") else ""
        self.autostart = autostart
        self.hwnd = None
        self.hicon = None
        self.edge: subprocess.Popen | None = None
        self.quitting = False
        self.close_action = "ask"
        self.auto_restart = True
        self.restart_count = 0
        self.started_at = time.time()
        self.tip = APP_NAME
        self._last_beat = 0.0
        self._shell = None
        self._asking = False

    # ---- shell 偏好 -------------------------------------------------------
    def _log(self, text: str) -> None:
        """托盘的错误只能写文件。

        pythonw 没有 stderr，窗口过程里抛的异常会被 pywin32 打到虚空——用户
        看到的就是「右键没反应」「点了没动静」。所以凡是走消息循环的入口都要
        留一行日志，出问题能查。
        """
        try:
            folder = DATA / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "tray.log"
            if path.is_file() and path.stat().st_size > 512 * 1024:
                path.replace(folder / "tray.log.1")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {text}\n")
        except OSError:
            pass

    def shell(self):
        if self._shell is None:
            try:
                from app.services import shell as shell_module
                self._shell = shell_module
            except Exception:                        # noqa: BLE001
                self._shell = False
        return self._shell or None

    def reload_prefs(self) -> None:
        shell = self.shell()
        if shell:
            try:
                data = shell.load()
                self.close_action = data.get("close_action") or "ask"
                self.auto_restart = bool(data.get("auto_restart", True))
            except Exception:                        # noqa: BLE001
                pass

    def window_mode(self, override: str = "") -> str:
        """这一次开哪种窗口。设置页改完立刻生效，不用重启托盘。

        顺序：命令行/菜单的显式指定 → data/shell.json 里的偏好 → 默认软件窗口。
        """
        if override in ("app", "browser"):
            return override
        if self.forced_mode:
            return self.forced_mode
        shell = self.shell()
        if shell:
            try:
                mode = str(shell.load().get("window_mode") or "")
                if mode in ("app", "browser"):
                    return mode
            except Exception:                        # noqa: BLE001
                pass
        return "app"

    def beat(self) -> None:
        shell = self.shell()
        if shell and time.time() - self._last_beat > 5:
            self._last_beat = time.time()
            try:
                shell.heartbeat()
            except Exception:                        # noqa: BLE001
                pass

    # ---- 服务 -------------------------------------------------------------
    def service_alive(self) -> bool:
        return _alive(_supervisor_pid())

    def start_service(self) -> bool:
        if self.service_alive():
            return True
        try:
            STOP.unlink(missing_ok=True)
        except OSError:
            pass
        supervisor = ROOT / "supervisor.pyw"
        if not supervisor.is_file():
            self.balloon("找不到 supervisor.pyw", "这个文件夹可能不完整，重装一次试试。")
            return False
        subprocess.Popen([str(_pythonw()), str(supervisor)], cwd=str(ROOT), env=_child_env(),
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return _wait_port()

    def stop_service(self, timeout: float = 25.0) -> bool:
        try:
            STOP.parent.mkdir(parents=True, exist_ok=True)
            STOP.write_text("stop", encoding="utf-8")
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.service_alive():
                return True
            time.sleep(0.5)
        return not self.service_alive()

    # ---- 窗口 -------------------------------------------------------------
    def open_browser_tab(self) -> None:
        """用默认浏览器开一个标签页。关掉标签页不影响服务，托盘继续留着。"""
        import webbrowser
        if not self.service_alive() and not self.start_service():
            self.balloon("后台服务没起来",
                         f"看这个文件最后几行：{DATA / 'logs' / 'app-api-console.log'}")
            return
        try:
            webbrowser.open(_url())
        except Exception as exc:                     # noqa: BLE001
            self.balloon("打开浏览器失败", f"{exc}\n也可以手动打开：{_url()}")

    def open_window(self, mode: str = "") -> None:
        if self.window_mode(mode) == "browser":
            self.open_browser_tab()
            return
        if self.edge and self.edge.poll() is None:
            # 窗口已经开着。Edge 的独立进程没法用 SetForegroundWindow 激活，
            # 再开一次只会被转发给同一个实例、新进程立刻退出，反而误触发
            # 「窗口已关闭」。所以这里什么都不做，用户从任务栏切过去即可。
            return
        if not self.service_alive():
            if not self.start_service():
                self.balloon("后台服务没起来",
                             f"看这个文件最后几行：{DATA / 'logs' / 'app-api-console.log'}")
                return
        edge = _edge_exe()
        if edge is None:
            self.open_browser_tab()
            return
        try:
            PROFILE.mkdir(parents=True, exist_ok=True)
            self.edge = subprocess.Popen(
                [str(edge), f"--app={_url()}", f"--user-data-dir={PROFILE}",
                 "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
                 "--window-size=1480,980"],
                cwd=str(ROOT), close_fds=True)
        except OSError as exc:
            self.balloon("打开窗口失败", f"{exc}\n已改用默认浏览器。")
            self.open_browser_tab()
            return
        proc = self.edge
        threading.Thread(target=self._watch_window, args=(proc,), daemon=True,
                         name="edge-watch").start()

    def _watch_window(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait()
        except Exception:                            # noqa: BLE001
            pass
        if not self.quitting and self.hwnd:
            try:
                import win32gui
                win32gui.PostMessage(self.hwnd, WM_WINDOW_CLOSED, 0, 0)
            except Exception:                        # noqa: BLE001
                pass

    # ---- 托盘图标 ---------------------------------------------------------
    def _icon_handle(self):
        import win32con, win32gui
        path = _make_icon()
        if path.is_file():
            try:
                return win32gui.LoadImage(0, str(path), win32con.IMAGE_ICON, 0, 0,
                                          win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
            except Exception:                        # noqa: BLE001
                pass
        return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _nid(self, flags: int):
        import win32gui
        return (self.hwnd, 0, flags, WM_TRAY, self.hicon, self.tip)

    def add_icon(self) -> None:
        import win32gui
        self.hicon = self._icon_handle()
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self._nid(
            win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP))

    def update_tip(self, tip: str) -> None:
        import win32gui
        if tip == self.tip:
            return
        self.tip = tip
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self._nid(
                win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP))
        except Exception:                            # noqa: BLE001
            pass

    def balloon(self, title: str, text: str) -> None:
        import win32gui
        try:
            # NIF_INFO 的元组比普通提示多两项：正文与超时；标题用 tip 这一格。
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (
                self.hwnd, 0, win32gui.NIF_INFO, WM_TRAY, self.hicon, title, text, 6000))
        except Exception:                            # noqa: BLE001
            pass

    def build_menu(self):
        """构造托盘菜单。

        抽成独立方法有两个原因：一是一次构造就能测（selftest 会真的跑一遍
        再销毁），二是「右键毫无反应」这类问题必须能被钉死——2026-11-22 的
        事故就是这里 `AppendMenu(MF_SEPARATOR, 0, None)` 抛 TypeError，而
        pythonw 没有 stderr，异常被吞得一点声音都没有。

        注意：分隔符的文本必须传空串。pywin32 不接受 None，会直接抛。
        """
        import win32con, win32gui
        menu = win32gui.CreatePopupMenu()
        try:
            alive = self.service_alive()
            mode = self.window_mode()
            autostart = self._autostart_on()
            open_on_start = self._open_on_start()

            def add(flags, item, text):
                win32gui.AppendMenu(menu, flags | win32con.MF_STRING, item, text)

            add(0, ID_OPEN, MENU_TEXT[ID_OPEN])
            add(win32con.MF_GRAYED, 0, "打开方式")
            add(win32con.MF_CHECKED if mode == "app" else 0, ID_APPWIN, MENU_TEXT[ID_APPWIN])
            add(win32con.MF_CHECKED if mode == "browser" else 0, ID_BROWSER, MENU_TEXT[ID_BROWSER])
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            add(win32con.MF_GRAYED, ID_STATUS, "服务：运行中" if alive else "服务：已停止")
            add(0, ID_RESTART, MENU_TEXT[ID_RESTART])
            add(0, ID_STOP, MENU_TEXT[ID_STOP])
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            add(win32con.MF_GRAYED, 0, "关窗口时")
            add(win32con.MF_CHECKED if self.close_action == "ask" else 0,
                ID_CLOSE_ASK, MENU_TEXT[ID_CLOSE_ASK])
            add(win32con.MF_CHECKED if self.close_action == "tray" else 0,
                ID_CLOSE_TRAY, MENU_TEXT[ID_CLOSE_TRAY])
            add(win32con.MF_CHECKED if self.close_action == "quit" else 0,
                ID_CLOSE_QUIT, MENU_TEXT[ID_CLOSE_QUIT])
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            add(win32con.MF_CHECKED if autostart else 0, ID_AUTOSTART, MENU_TEXT[ID_AUTOSTART])
            add(win32con.MF_CHECKED if open_on_start else 0, ID_OPENWIN, MENU_TEXT[ID_OPENWIN])
            add(0, ID_DATA, MENU_TEXT[ID_DATA])
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            add(0, ID_QUIT, MENU_TEXT[ID_QUIT])
        except Exception:
            win32gui.DestroyMenu(menu)
            raise
        return menu

    def show_menu(self) -> None:
        import win32con, win32gui
        try:
            menu = self.build_menu()
        except Exception as exc:                     # noqa: BLE001
            self._log(f"菜单构造失败：{exc!r}")
            self.balloon("托盘菜单打不开", f"{exc}\n详情见 data/logs/tray.log")
            return
        choice = 0
        try:
            x, y = win32gui.GetCursorPos()
            # 没有这一步，菜单会因为窗口不在前台而立刻失焦消失——表现同样是
            # 「右键点了没反应」。点图标本身是输入事件，前台权限在这里是给的。
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:                        # noqa: BLE001
                pass
            choice = win32gui.TrackPopupMenu(
                menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON | win32con.TPM_RETURNCMD,
                x, y, 0, self.hwnd, None)
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        except Exception as exc:                     # noqa: BLE001
            self._log(f"弹出菜单失败：{exc!r}")
        finally:
            win32gui.DestroyMenu(menu)
        if choice:
            try:
                self.on_command(int(choice))
            except Exception as exc:                 # noqa: BLE001
                self._log(f"菜单命令 {choice} 执行失败：{exc!r}")
                self.balloon("操作失败", str(exc))

    def _open_on_start(self) -> bool:
        shell = self.shell()
        if shell:
            try:
                return bool(shell.load().get("open_on_start"))
            except Exception:                        # noqa: BLE001
                pass
        return False

    def _autostart_on(self) -> bool:
        try:
            from app.services import autostart
            return bool(autostart.status().get("enabled"))
        except Exception:                            # noqa: BLE001
            return False

    def set_window_mode(self, mode: str) -> None:
        """托盘菜单里选「软件窗口 / 浏览器标签页」＝ 改偏好，下次双击 exe 也按这个来。"""
        shell = self.shell()
        if shell:
            try:
                shell.save(window_mode=mode)
            except Exception:                        # noqa: BLE001
                pass
        self.balloon(APP_NAME, "以后打开界面用" + ("软件窗口" if mode == "app" else "浏览器标签页"))

    def on_command(self, cmd: int) -> None:
        if cmd == ID_OPEN:
            self.open_window()
        elif cmd == ID_APPWIN:
            self.set_window_mode("app")
            self.open_window("app")
        elif cmd == ID_BROWSER:
            self.set_window_mode("browser")
            self.open_window("browser")
        elif cmd == ID_RESTART:
            self.restart_service()
        elif cmd == ID_STOP:
            if self.stop_service():
                self.update_tip(f"{APP_NAME} · 服务已停止")
                self.balloon(APP_NAME, "后台服务已停止，托盘还留着。双击图标可以重新启动。")
            else:
                self.balloon("停止超时", "supervisor 没有在 25 秒内退出，看任务管理器里还有没有 pythonw.exe。")
        elif cmd in (ID_CLOSE_ASK, ID_CLOSE_TRAY, ID_CLOSE_QUIT):
            wanted = {ID_CLOSE_ASK: "ask", ID_CLOSE_TRAY: "tray",
                      ID_CLOSE_QUIT: "quit"}[cmd]
            self.set_close_action(wanted)
            self.balloon(APP_NAME, {
                "ask": "以后关窗口会先问你一句",
                "tray": "以后关窗口只关窗口，服务留在托盘",
                "quit": "以后关窗口会连后台服务一起退出",
            }[wanted])
        elif cmd == ID_AUTOSTART:
            self.toggle_autostart()
        elif cmd == ID_OPENWIN:
            wanted = not self._open_on_start()
            shell = self.shell()
            if shell:
                try:
                    shell.save(open_on_start=wanted)
                except Exception:                    # noqa: BLE001
                    pass
            self.balloon(APP_NAME, "以后开机自启会" + ("打开界面" if wanted else "只把图标放进托盘，不弹窗口"))
        elif cmd == ID_DATA:
            try:
                os.startfile(str(DATA))              # noqa: S606 - 用户自己的数据目录
            except OSError:
                pass
        elif cmd == ID_QUIT:
            self.quit_app()

    def set_close_action(self, value: str) -> None:
        if value not in ("ask", "tray", "quit"):
            value = "ask"
        self.close_action = value
        shell = self.shell()
        if shell:
            try:
                shell.save(close_action=value)
            except Exception:                        # noqa: BLE001
                pass

    def toggle_autostart(self) -> None:
        try:
            from app.services import autostart
            if autostart.status().get("enabled"):
                autostart.disable()
                self.balloon(APP_NAME, "已取消开机自启")
            else:
                autostart.enable()
                self.balloon(APP_NAME, "已设为开机自启：" + (
                    "登录后自动起服务并打开界面" if self._open_on_start()
                    else "登录后自动起服务，图标进托盘、不弹窗口"))
        except Exception as exc:                     # noqa: BLE001
            self.balloon("改开机自启失败", str(exc))

    def restart_service(self) -> None:
        self.quitting = False
        self.stop_service()
        time.sleep(1.0)
        if self.start_service():
            self.update_tip(f"{APP_NAME} · 服务运行中")
            self.balloon(APP_NAME, "服务已重启")
            self.open_window()
        else:
            self.balloon("重启失败", "服务没能起来，看 data/logs/supervisor.log")

    def quit_app(self) -> None:
        """退出分两段：先把「停服务」扔到线程里，窗口那条线程继续泵消息，
        否则关窗那一下会卡住十几秒，Windows 会给图标盖上「未响应」。"""
        if self.quitting:
            return
        self.quitting = True
        threading.Thread(target=self._shutdown, daemon=True, name="tray-shutdown").start()

    def _shutdown(self) -> None:
        import win32gui
        if self.with_service:
            self.stop_service()
        if self.edge and self.edge.poll() is None:
            try:
                self.edge.terminate()
            except OSError:
                pass
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:                            # noqa: BLE001
            pass
        try:
            win32gui.PostMessage(self.hwnd, 0x0010, 0, 0)   # WM_CLOSE → 主线程销毁窗口
        except Exception:                            # noqa: BLE001
            pass

    # ---- 消息循环 ---------------------------------------------------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        import win32gui
        try:
            return self._handle_message(hwnd, msg, wparam, lparam)
        except Exception as exc:                     # noqa: BLE001
            # 托盘窗口过程抛异常时 pywin32 只会往 stderr 打一行，而 pythonw
            # 没有 stderr。不接住就等于「点了没反应」，所以这里必须留痕。
            self._log(f"消息 {msg:#06x} 处理失败：{exc!r}")
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _handle_message(self, hwnd, msg, wparam, lparam):
        import win32con, win32gui
        if msg == WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event == win32con.WM_LBUTTONDBLCLK:
                self.open_window()
            elif event in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                self.show_menu()
            return 0
        if msg == WM_WINDOW_CLOSED:
            self.edge = None
            if self.close_action == "quit":
                self.quit_app()
            elif self.close_action == "ask":
                self.ask_about_close()
            else:
                self.balloon(APP_NAME, "窗口已关闭，服务还在后台跑。双击托盘图标可以重新打开。")
            return 0
        if msg == WM_TICK:
            self.tick()
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        if msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ---- 关窗询问 ---------------------------------------------------------
    def ask_about_close(self) -> None:
        """关了窗口之后问一句：要不要连后台服务一起退。

        MessageBox 必须在别的线程里弹：它自己会泵消息，在窗口过程中同步调用
        会把托盘的消息循环卡住，图标当场变「未响应」。同一个时间只问一次，
        连着关两次窗不会叠出两个框。
        """
        if self._asking or self.quitting:
            return
        self._asking = True
        threading.Thread(target=self._ask_about_close, daemon=True,
                         name="tray-ask-close").start()

    def _ask_about_close(self) -> None:
        import win32con, win32gui
        try:
            answer = win32gui.MessageBox(
                self.hwnd,
                "界面窗口关掉了，后台服务还在跑。\n\n"
                "「是」：连后台服务一起退出，采集也停。\n"
                "「否」：只关窗口，服务留在右下角托盘里，\n"
                "　　　　双击托盘图标就能重新打开。\n\n"
                "不想每次都问：设置 → 桌面程序 →「关闭窗口时」选一项。",
                APP_NAME,
                win32con.MB_YESNO | win32con.MB_ICONQUESTION
                | win32con.MB_TOPMOST | win32con.MB_SETFOREGROUND)
        except Exception as exc:                     # noqa: BLE001
            self._log(f"关窗询问失败：{exc!r}")
            answer = win32con.IDNO
        finally:
            self._asking = False
        if answer == win32con.IDYES:
            self.quit_app()
        else:
            self.balloon(APP_NAME, "服务还在后台跑。双击托盘图标可以重新打开界面。")

    def tick(self) -> None:
        self.beat()
        self.reload_prefs()
        shell = self.shell()
        if shell:
            try:
                cmd = shell.take()
            except Exception:                        # noqa: BLE001
                cmd = ""
            if cmd == "open":
                self.open_window()
            elif cmd == "quit":
                self.quit_app()
                return
            elif cmd == "restart":
                self.restart_service()
        if self.service_alive():
            self.restart_count = 0
            self.update_tip(f"{APP_NAME} · 服务运行中")
            return
        if self.quitting:
            return
        # 用户自己点了「停止服务」或「Stop Radar.vbs」时不抢着拉起，尊重那个动作。
        if STOP.exists():
            self.update_tip(f"{APP_NAME} · 服务已停止")
            return
        if self.auto_restart and self.restart_count < 3:
            self.restart_count += 1
            if self.start_service():
                self.update_tip(f"{APP_NAME} · 服务已自动恢复")
            else:
                self.update_tip(f"{APP_NAME} · 服务启动失败")
        else:
            self.update_tip(f"{APP_NAME} · 服务已停止")

    def should_open_window(self) -> bool:
        """这次启动要不要把界面窗口拉出来。

        * 双击图标（没有 --autostart）＝要，用户点它就是想看界面；
        * 开机自启（--autostart）＝看设置里的「开机启动时打开界面」；
        * --background ＝一律不开，命令行和测试用。
        """
        if self.background:
            return False
        if self.autostart:
            return self._open_on_start()
        return True

    def run(self) -> int:
        import win32con, win32gui
        self.reload_prefs()
        hinst = win32gui.GetModuleHandle(None)
        class_name = "HermesRadarTrayWindow"
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        wc.lpfnWndProc = self._wndproc
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_WINDOW
        wc.hIcon = self._icon_handle()
        win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(class_name, APP_NAME, win32con.WS_OVERLAPPED,
                                          0, 0, 0, 0, 0, 0, hinst, None)
        self.add_icon()
        self.beat()

        if self.with_service and not self.service_alive():
            self.start_service()
        self.update_tip(f"{APP_NAME} · {'服务运行中' if self.service_alive() else '服务未启动'}")

        if self.should_open_window():
            threading.Thread(target=self._open_after_ready, daemon=True, name="open-window").start()

        threading.Thread(target=self._ticker, daemon=True, name="ticker").start()
        win32gui.PumpMessages()
        return 0

    def _open_after_ready(self) -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not self.quitting:
            if self.service_alive() and _wait_port(2.0):
                break
            time.sleep(0.5)
        if not self.quitting:
            self.open_window()

    def _ticker(self) -> None:
        import win32gui
        while not self.quitting:
            time.sleep(2.0)
            try:
                win32gui.PostMessage(self.hwnd, WM_TICK, 0, 0)
            except Exception:                        # noqa: BLE001
                return


# --------------------------------------------------------------------------- #
def _single_instance() -> bool:
    """同一安装目录只允许一个托盘。已经有一个在跑就让它开窗口，自己退出。"""
    import hashlib
    tag = hashlib.sha1(str(ROOT).casefold().encode("utf-8")).hexdigest()[:12]
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\HermesRadarTray-{tag}")
    if ctypes.windll.kernel32.GetLastError() == 183:
        try:
            from app.services import shell
            shell.send("open")
        except Exception:                            # noqa: BLE001
            pass
        return False
    globals()["_MUTEX_HANDLE"] = handle
    return True


def _selftest() -> int:
    """冒烟测试：构造菜单 → 建图标 → 跑 4 秒消息循环 → 拆掉。

    菜单必须真的构造一次。2026-11-22 那次「右键图标毫无反应」的根因就在这里
    （`AppendMenu(MF_SEPARATOR, 0, None)` 抛 TypeError），而 pythonw 没有
    stderr，异常被吞得干干净净。失败时返回非 0，打包与升级脚本能拿它当门禁。

    刻意 ``with_service=False``：自检绝不能碰正在跑的服务，它只验证托盘本身
    能建起来、菜单能构造、退出路径不发散。
    """
    import win32gui
    app = TrayApp(background=True, with_service=False)
    try:
        menu = app.build_menu()
        count = win32gui.GetMenuItemCount(menu)
        win32gui.DestroyMenu(menu)
        if count <= 0:
            app._log("selftest：菜单是空的")
            return 4
    except Exception as exc:                        # noqa: BLE001
        app._log(f"selftest：菜单构造失败 {exc!r}")
        return 3
    threading.Thread(target=lambda: (time.sleep(4), app.quit_app()), daemon=True).start()
    app.run()
    return 0


def main(argv: list[str]) -> int:
    args = {item.lower().lstrip("-") for item in argv[1:]}
    if "selftest" in args:
        return _selftest()
    if not _single_instance():
        return 0
    mode = "browser" if "browser" in args else ("app" if "app" in args else "")
    app = TrayApp(background=("background" in args), with_service=("no-service" not in args),
                  mode=mode, autostart=("autostart" in args))
    return app.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception as exc:                         # pythonw 没有控制台，异常必须留痕
        try:
            with (DATA / "logs" / "tray.log").open("a", encoding="utf-8") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 托盘异常：{type(exc).__name__}: {exc}\n")
        except OSError:
            pass
        raise SystemExit(1)
