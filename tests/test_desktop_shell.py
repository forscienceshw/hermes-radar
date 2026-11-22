"""双击入口的回归测试：开「软件窗口」还是「浏览器标签页」。

2026-11-22 之前只有 VBS → 托盘一条路，窗口形式写死在代码里。现在双击的是
Radar.exe，设置页与托盘菜单都能改打开方式；这里锁住三件事：

* 默认是软件窗口——用户装的是软件，不是书签；
* 偏好存在 data/shell.json 的 window_mode，改了立刻生效；
* 显式指定（托盘菜单里点某一项、命令行 --browser）优先于偏好。
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import shell

ROOT = Path(__file__).resolve().parents[1]


def _load_tray():
    """radar_tray.pyw 不是 .py，按路径加载；它 import 阶段不碰 win32。"""
    spec = importlib.util.spec_from_file_location("radar_tray", ROOT / "radar_tray.pyw")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WindowModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tray = _load_tray()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        for name, value in (("SHELL_FILE", base / "shell.json"),
                            ("CMD_FILE", base / "shell_cmd.json"),
                            ("ALIVE_FILE", base / "shell_alive.json")):
            ctx = patch.object(shell, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)

    def _app(self, **kwargs):
        # 只创建一个不碰服务的实例：__init__ 只赋属性，不建窗口、不起进程。
        return self.tray.TrayApp(background=True, with_service=False, **kwargs)

    def test_default_is_app_window(self):
        self.assertEqual(shell.load()["window_mode"], "app")
        self.assertEqual(self._app().window_mode(), "app")

    def test_preference_is_honoured(self):
        shell.save(window_mode="browser")
        self.assertEqual(self._app().window_mode(), "browser")

    def test_explicit_mode_beats_preference(self):
        shell.save(window_mode="browser")
        app = self._app()
        self.assertEqual(app.window_mode("app"), "app")
        shell.save(window_mode="app")
        self.assertEqual(app.window_mode("browser"), "browser")

    def test_command_line_override(self):
        shell.save(window_mode="browser")
        # 托盘是 --app 起的（例如开机自启想弹软件窗口），别被旧偏好盖掉
        self.assertEqual(self._app(mode="app").window_mode(), "app")

    def test_garbage_preference_falls_back(self):
        (Path(self.temp.name) / "shell.json").write_text(
            '{"window_mode": "modal", "close_action": "tray"}', "utf-8")
        self.assertEqual(self._app().window_mode(), "app")


class CloseActionTest(unittest.TestCase):
    """点窗口右上角的叉之后干什么：问一句 / 只关窗口 / 连服务一起退。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        for name, value in (("SHELL_FILE", base / "shell.json"),
                            ("CMD_FILE", base / "shell_cmd.json"),
                            ("ALIVE_FILE", base / "shell_alive.json")):
            ctx = patch.object(shell, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)

    def test_default_is_ask(self):
        # 默认问一句：用户第一次关窗口时能自己选，而不是被默默替决定。
        self.assertEqual(shell.load()["close_action"], "ask")

    def test_three_states_round_trip(self):
        for value in shell.CLOSE_ACTIONS:
            shell.save(close_action=value)
            self.assertEqual(shell.load()["close_action"], value)

    def test_garbage_falls_back_to_ask(self):
        shell.save(close_action="whatever")
        self.assertEqual(shell.load()["close_action"], "ask")

    def test_tray_reads_preference_on_tick(self):
        tray = _load_tray()
        app = tray.TrayApp(background=True, with_service=False)
        shell.save(close_action="quit")
        app.reload_prefs()
        self.assertEqual(app.close_action, "quit")
        shell.save(close_action="ask")
        app.reload_prefs()
        self.assertEqual(app.close_action, "ask")

    def test_window_closed_dispatches_by_preference(self):
        """点叉之后走哪条路，只看 close_action。

        这三条分支是用户最直接的体感：不弹框、弹框、直接退。任何一个分派错
        都会让「我只想关窗口」变成「采集全停了」或者反过来。
        """
        tray = _load_tray()
        for value, expect_ask, expect_quit in (("ask", True, False),
                                               ("tray", False, False),
                                               ("quit", False, True)):
            app = tray.TrayApp(background=True, with_service=False)
            app.close_action = value
            asked, quit_calls = [], []
            app.ask_about_close = lambda: asked.append(1)
            app.quit_app = lambda: quit_calls.append(1)
            app.balloon = lambda *a, **k: None
            app._handle_message(None, tray.WM_WINDOW_CLOSED, 0, 0)
            self.assertEqual(bool(asked), expect_ask, f"{value} 是否弹询问")
            self.assertEqual(bool(quit_calls), expect_quit, f"{value} 是否直接退出")


class WebViewClosingTest(unittest.TestCase):
    """软件窗口（WebView2）点右上角叉的那一刻。

    2026-11-23 之前窗口是 Edge 的 --app，属于别人的进程：点叉之后它立刻退出，
    我们只能事后发现再弹确认框，用户看到的是「主界面先没了、框才出来」，还
    会卡一下。换成自己的窗口之后，closing 回调必须**一律返回 False** 把关闭
    挡住，等用户选完再由我们自己隐藏或销毁。这里锁住这条规矩。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        for name, value in (("SHELL_FILE", base / "shell.json"),
                            ("CMD_FILE", base / "shell_cmd.json"),
                            ("ALIVE_FILE", base / "shell_alive.json")):
            ctx = patch.object(shell, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        self.tray = _load_tray()

    def _app(self):
        app = self.tray.TrayApp(background=True, with_service=False)
        app.ask_about_close = lambda: None
        app.quit_app = lambda: None
        return app

    def test_close_is_always_blocked_first(self):
        """三种偏好都先返回 False：窗口不能抢在确认框前面消失。"""
        for value in ("ask", "tray", "quit"):
            app = self._app()
            app.close_action = value
            self.assertFalse(app._on_window_closing(), f"{value} 不该让窗口直接关掉")

    def test_ask_starts_the_question(self):
        app = self._app()
        asked = []
        app.ask_about_close = lambda: asked.append(1)
        app.close_action = "ask"
        app._on_window_closing()
        self.assertEqual(asked, [1])

    def test_quit_stops_everything(self):
        app = self._app()
        quits = []
        app.quit_app = lambda: quits.append(1)
        app.close_action = "quit"
        app._on_window_closing()
        self.assertEqual(quits, [1])

    def test_tray_keeps_the_service(self):
        app = self._app()
        quits, hides = [], []
        app.quit_app = lambda: quits.append(1)
        app._hide_window = lambda: hides.append(1)
        app.close_action = "tray"
        app._on_window_closing()
        self.assertEqual(quits, [])
        self.assertFalse(app._force_close)

    def test_forced_close_lets_it_go(self):
        """退出流程里必须放行，否则 window.destroy() 会被自己挡住，进程卡死。"""
        app = self._app()
        app._force_close = True
        self.assertTrue(app._on_window_closing())


class AppWindowUrlTest(unittest.TestCase):
    """软件窗口的 URL 要带 ?app=1，网页据此把外部链接交给系统浏览器。"""

    def setUp(self):
        self.tray = _load_tray()

    def test_app_window_carries_marker(self):
        self.assertTrue(self.tray._url(app_window=True).endswith("/?app=1"))

    def test_browser_tab_stays_clean(self):
        self.assertNotIn("app=1", self.tray._url())


class OpenWindowTest(unittest.TestCase):
    """open_window 只表达意愿，不自己起窗口。"""

    def setUp(self):
        self.tray = _load_tray()

    def test_app_mode_defers_to_main_thread(self):
        # pywebview 硬性要求主线程 start，所以 open_window 里不能直接建窗口。
        app = self.tray.TrayApp(background=True, with_service=False, mode="app")
        app.service_alive = lambda: True
        app.open_window()
        self.assertTrue(app._want_window)

    def test_existing_window_is_shown_not_recreated(self):
        app = self.tray.TrayApp(background=True, with_service=False, mode="app")
        shown = []
        app.win = object()
        app._show_window = lambda: shown.append(1)
        app.open_window()
        self.assertEqual(shown, [1])
        self.assertFalse(app._want_window)


class OpenUrlTest(unittest.TestCase):
    """软件窗口里的「打开原文」要把链接交给系统浏览器。

    接口只认 http/https：这个端点最终会拉起本机上的程序，别让它变成
    「网页点什么就开什么」的通道。
    """

    def test_http_is_forwarded_to_the_browser(self):
        from app.api import system_open_url
        opened = []
        with patch("os.startfile", lambda url: opened.append(url)):
            result = system_open_url({"url": "http://127.0.0.1:8766/api/items/1/archive"})
        self.assertTrue(result["ok"])
        self.assertEqual(opened, ["http://127.0.0.1:8766/api/items/1/archive"])

    def test_other_schemes_are_rejected(self):
        from fastapi import HTTPException
        from app.api import system_open_url
        for bad in ("file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", "", "   "):
            with self.assertRaises(HTTPException):
                system_open_url({"url": bad})

    def test_relative_url_is_rejected(self):
        from fastapi import HTTPException
        from app.api import system_open_url
        with self.assertRaises(HTTPException):
            system_open_url({"url": "/api/items/1/archive"})


class OpenOnStartTest(unittest.TestCase):
    """开机自启要不要弹窗口。默认安静：登录时不抢焦点。"""

    @classmethod
    def setUpClass(cls):
        cls.tray = _load_tray()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        for name, value in (("SHELL_FILE", base / "shell.json"),
                            ("CMD_FILE", base / "shell_cmd.json"),
                            ("ALIVE_FILE", base / "shell_alive.json")):
            ctx = patch.object(shell, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)

    def test_default_is_quiet(self):
        self.assertFalse(shell.load()["open_on_start"])

    def test_toggle(self):
        shell.save(open_on_start=True)
        self.assertTrue(shell.load()["open_on_start"])
        shell.save(open_on_start=False)
        self.assertFalse(shell.load()["open_on_start"])

    def test_double_click_always_opens(self):
        shell.save(open_on_start=False)
        app = self.tray.TrayApp(background=False, with_service=False)
        self.assertTrue(app.should_open_window())

    def test_autostart_honours_preference(self):
        app = self.tray.TrayApp(background=False, with_service=False, autostart=True)
        shell.save(open_on_start=False)
        self.assertFalse(app.should_open_window())
        shell.save(open_on_start=True)
        self.assertTrue(app.should_open_window())

    def test_background_never_opens(self):
        app = self.tray.TrayApp(background=True, with_service=False, autostart=True)
        shell.save(open_on_start=True)
        self.assertFalse(app.should_open_window())


class TrayMenuTest(unittest.TestCase):
    """托盘右键菜单必须真的能构造出来。

    2026-11-22 的事故：`AppendMenu(MF_SEPARATOR, 0, None)` 抛
    `TypeError: None is not a valid string in this context`，而托盘跑在
    pythonw 里、没有 stderr，于是用户看到的只是「右键图标毫无反应」。
    这条测试就是那次事故的钉子：分隔符一个都不能再传 None。
    """

    @classmethod
    def setUpClass(cls):
        cls.tray = _load_tray()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        for name, value in (("SHELL_FILE", base / "shell.json"),
                            ("CMD_FILE", base / "shell_cmd.json"),
                            ("ALIVE_FILE", base / "shell_alive.json")):
            ctx = patch.object(shell, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)

    def test_menu_builds(self):
        import win32gui
        app = self.tray.TrayApp(background=True, with_service=False)
        menu = app.build_menu()
        try:
            # 19 项含 4 条分隔线。数字写死是故意的：少一项就是漏了菜单。
            self.assertEqual(win32gui.GetMenuItemCount(menu), 19)
        finally:
            win32gui.DestroyMenu(menu)

    def test_every_action_has_text(self):
        import ctypes
        import win32con, win32gui

        def menu_text(menu, pos):
            # pywin32 没包 GetMenuString，直接问 user32。
            buffer = ctypes.create_unicode_buffer(128)
            ctypes.windll.user32.GetMenuStringW(int(menu), pos, buffer, 128,
                                                win32con.MF_BYPOSITION)
            return buffer.value

        app = self.tray.TrayApp(background=True, with_service=False)
        menu = app.build_menu()
        try:
            texts = {}
            for pos in range(win32gui.GetMenuItemCount(menu)):
                item = win32gui.GetMenuItemID(menu, pos)
                if item:                       # 0 = 分隔线或灰标题
                    texts[item] = menu_text(menu, pos)
            for item in (self.tray.ID_OPEN, self.tray.ID_APPWIN, self.tray.ID_BROWSER,
                         self.tray.ID_RESTART, self.tray.ID_STOP,
                         self.tray.ID_CLOSE_ASK, self.tray.ID_CLOSE_TRAY,
                         self.tray.ID_CLOSE_QUIT, self.tray.ID_AUTOSTART,
                         self.tray.ID_OPENWIN, self.tray.ID_DATA, self.tray.ID_QUIT):
                self.assertTrue(texts.get(item), f"菜单项 {item} 没有文字")
        finally:
            win32gui.DestroyMenu(menu)

    def test_close_action_checks_the_right_item(self):
        """三态里只有当前那一项带勾。"""
        import win32con, win32gui
        app = self.tray.TrayApp(background=True, with_service=False)
        for value, item in (("ask", self.tray.ID_CLOSE_ASK),
                            ("tray", self.tray.ID_CLOSE_TRAY),
                            ("quit", self.tray.ID_CLOSE_QUIT)):
            app.close_action = value
            menu = app.build_menu()
            try:
                flags = {pos: win32gui.GetMenuState(menu, pos, win32con.MF_BYPOSITION)
                         for pos in range(win32gui.GetMenuItemCount(menu))}
                checked = [win32gui.GetMenuItemID(menu, pos)
                           for pos, state in flags.items()
                           if state & win32con.MF_CHECKED
                           and win32gui.GetMenuItemID(menu, pos) in (
                               self.tray.ID_CLOSE_ASK, self.tray.ID_CLOSE_TRAY,
                               self.tray.ID_CLOSE_QUIT)]
                self.assertEqual(checked, [item], f"{value} 的勾落错了位置")
            finally:
                win32gui.DestroyMenu(menu)


if __name__ == "__main__":
    unittest.main()
