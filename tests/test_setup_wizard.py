"""设置向导相关的单元测试。

只测不花钱、不联网、不改用户环境的那部分：
自启动命令拼装、大模型测试的入参校验、4.x 密钥正则。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import autostart, setup_test, wechat4  # noqa: E402


class AutostartTests(unittest.TestCase):
    def test_prefers_bundled_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "Radar.exe").write_bytes(b"MZ")
            with patch.object(autostart, "BASE_DIR", base):
                command = autostart.launcher_command()
            self.assertEqual(command[0], str(base / "Radar.exe"))
            self.assertEqual(command[-1], "start")

    def test_falls_back_to_pythonw(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scripts = base / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            pythonw = scripts / "pythonw.exe"
            pythonw.write_bytes(b"MZ")
            (base / "supervisor.pyw").write_text("", encoding="utf-8")
            with patch.object(autostart, "BASE_DIR", base):
                command = autostart.launcher_command()
            self.assertEqual(command, [str(pythonw), str(base / "supervisor.pyw")])

    def test_status_shape(self):
        result = autostart.status()
        for key in ("enabled", "mode", "command", "target", "supported"):
            self.assertIn(key, result)
        self.assertIn(result["mode"], ("registry", "legacy_shortcut", "off"))


class LlmTestInputTests(unittest.TestCase):
    def test_missing_key_is_a_message_not_an_exception(self):
        result = asyncio.run(setup_test.test_llm("openai_compatible", "https://api.deepseek.com/v1",
                                                 "deepseek-chat", ""))
        self.assertFalse(result["ok"])
        self.assertIn("Key", result["message"])

    def test_missing_base_url(self):
        result = asyncio.run(setup_test.test_llm("openai_compatible", "", "deepseek-chat", "sk-x"))
        self.assertFalse(result["ok"])
        self.assertIn("Base URL", result["message"])

    def test_normalise_strips_endpoint_and_slash(self):
        self.assertEqual(setup_test._normalise("https://a.com/v1/"), "https://a.com/v1")
        self.assertEqual(setup_test._normalise("https://a.com/v1/chat/completions"), "https://a.com/v1")
        self.assertEqual(setup_test._normalise("   "), "")


class Wechat4KeyRegexTests(unittest.TestCase):
    def test_finds_key_in_a_dirty_buffer(self):
        blob = b"\x00\x01com.Tencent.WCDB.Config.Cipher\x00x'" + b"a1" * 32 + b"'\x00trailing"
        found = wechat4.KEY_PATTERN.findall(blob)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].decode(), "a1" * 32)

    def test_ignores_short_or_non_hex(self):
        self.assertEqual(wechat4.KEY_PATTERN.findall(b"x'abc'"), [])
        self.assertEqual(wechat4.KEY_PATTERN.findall(b"x'" + b"z" * 64 + b"'"), [])


class SnapshotCacheTests(unittest.TestCase):
    """体检快照默认复用 90 秒内的结果，只有 refresh 才重算。

    冷算一次约六秒：要读一次微信进程、扫一遍磁盘。设置页以前每次打开都算，
    体感就是「点一下卡半天」——所以默认进参数设置，向导按需加载，后端再加这层缓存。
    """

    def setUp(self):
        from app.services import setup_status
        self.mod = setup_status
        self.mod._SNAPSHOT_CACHE = None
        self.addCleanup(setattr, self.mod, "_SNAPSHOT_CACHE", None)

    def test_reuses_a_recent_snapshot(self):
        calls = []
        with patch.object(self.mod, "_build_snapshot",
                          side_effect=lambda: calls.append(1) or {"n": len(calls)}):
            first = self.mod.snapshot()
            second = self.mod.snapshot()
        self.assertEqual(len(calls), 1)
        self.assertIs(first, second)

    def test_force_rebuilds_even_when_fresh(self):
        with patch.object(self.mod, "_build_snapshot", return_value={"ok": True}) as build:
            self.mod.snapshot()
            self.mod.snapshot(force=True)
        self.assertEqual(build.call_count, 2)

    def test_expired_cache_is_rebuilt(self):
        with patch.object(self.mod, "_build_snapshot", return_value={"ok": True}) as build:
            self.mod.snapshot()
            stamp, data = self.mod._SNAPSHOT_CACHE
            self.mod._SNAPSHOT_CACHE = (stamp - self.mod.SNAPSHOT_TTL_SECONDS - 1, data)
            self.mod.snapshot()
        self.assertEqual(build.call_count, 2)


class SettingsPageTests(unittest.TestCase):
    """界面上的两条硬约定，用文本锁住，避免以后又被改回去。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_params_tab_is_the_default(self):
        # 打开设置先看参数；配置向导只在真的切过去时才体检。
        self.assertIn('class="subnav-btn active" data-subtab="params"', self.html)
        self.assertIn('id="subtab-wizard" hidden', self.html)
        # 顶栏那块在没体检之前只是一句「配置向导」，不要先摆个「– / 5 步」装作读到了。
        self.assertIn('id="setupScoreText"', self.html)

    def test_layers_that_must_float_above_the_reader_are_popovers(self):
        # 精读页是 showModal()，普通 div 的 z-index 压不过 top layer。
        for element_id in ("chatDrawer", "notePop", "selectionBar", "toast"):
            with self.subTest(element=element_id):
                pattern = rf'id="{element_id}"[^>]*popover="manual"'
                self.assertRegex(self.html, pattern)

    def test_reader_has_its_own_ask_entry(self):
        # 「问 AI」浮钮在 modal 底下点不到，精读页顶栏要有一条能点的入口。
        self.assertIn('id="detailAsk"', self.html)


if __name__ == "__main__":
    unittest.main()
