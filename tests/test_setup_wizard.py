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


if __name__ == "__main__":
    unittest.main()
