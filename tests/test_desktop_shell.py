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


if __name__ == "__main__":
    unittest.main()
