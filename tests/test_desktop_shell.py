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
