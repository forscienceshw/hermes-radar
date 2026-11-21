"""API Key 模型的 token 账本：只记自带 Key 的 OpenAI 兼容调用。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import api_usage


class ApiUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name) / "api-usage.jsonl"
        self.patch = patch.object(api_usage, "API_USAGE_LOG", self.log)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_workbuddy_and_ollama_are_not_recorded_here(self):
        api_usage.record(provider="workbuddy_cloud", model="hy4flash-free",
                         usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        api_usage.record(provider="ollama", model="qwen3:8b",
                         usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        self.assertFalse(self.log.exists())
        self.assertEqual(api_usage.summary()["total_tokens"], 0)

    def test_openai_compatible_call_is_counted(self):
        api_usage.record(provider="openai_compatible", model="deepseek-chat",
                         base_url="https://api.deepseek.com/v1",
                         usage={"prompt_tokens": 1200, "completion_tokens": 300,
                                "total_tokens": 1500},
                         elapsed_ms=2400, task="analyze", account="default", label="DeepSeek")
        summary = api_usage.summary()
        self.assertEqual(summary["total_tokens"], 1500)
        self.assertEqual(summary["today_tokens"], 1500)
        self.assertEqual(summary["total_calls"], 1)
        self.assertEqual(summary["by_model"][0]["model"], "deepseek-chat")
        self.assertEqual(summary["by_model"][0]["label"], "DeepSeek")
        self.assertEqual(summary["by_model"][0]["prompt_tokens"], 1200)
        self.assertEqual(summary["by_model"][0]["avg_seconds"], 2.4)

    def test_missing_usage_is_skipped_instead_of_writing_zero_rows(self):
        api_usage.record(provider="openai_compatible", model="x", usage={})
        self.assertEqual(api_usage.summary()["total_calls"], 0)

    def test_total_is_derived_when_only_parts_exist(self):
        api_usage.record(provider="openai_compatible", model="kimi",
                         usage={"prompt_tokens": 30, "completion_tokens": 12})
        summary = api_usage.summary()
        self.assertEqual(summary["total_tokens"], 42)
        self.assertEqual(summary["completion_tokens"], 12)

    def test_recent_calls_are_newest_first(self):
        for tokens in (10, 20):
            api_usage.record(provider="openai_compatible", model=f"m{tokens}",
                             usage={"total_tokens": tokens})
        rows = api_usage.recent_calls(5)
        self.assertEqual(rows[0]["model"], "m20")
        self.assertEqual(rows[1]["model"], "m10")


if __name__ == "__main__":
    unittest.main()
