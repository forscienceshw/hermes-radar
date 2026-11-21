import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.pipeline import JobRunner, source_fatal_error

ROOT = Path(__file__).resolve().parents[1]


def _temp_root() -> Path:
    root = ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


class FakeHTTPError(Exception):
    """httpx.HTTPStatusError 的最小替身：只需要 response.status_code 与那句固定文案。"""

    def __init__(self, code: int, phrase: str):
        self.response = type("Response", (), {"status_code": code})()
        super().__init__(f"Client error '{code} {phrase}' for url 'https://api.dataxapi.com/v1/x'")


class SourceFatalErrorTests(unittest.TestCase):
    def test_payment_required_is_fatal(self):
        hit = source_fatal_error(FakeHTTPError(402, "Payment Required"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], 402)
        self.assertIn("余额不足", hit[1])
        self.assertIn("充值", hit[2])

    def test_unauthorized_is_fatal(self):
        self.assertEqual(source_fatal_error(FakeHTTPError(401, "Unauthorized"))[0], 401)

    def test_forbidden_is_fatal(self):
        self.assertEqual(source_fatal_error(FakeHTTPError(403, "Forbidden"))[0], 403)

    def test_text_only_error_still_recognised(self):
        """有些包装会吃掉 response，只剩文案；这句文案是 httpx 的固定格式。"""
        hit = source_fatal_error(RuntimeError("Client error '402 Payment Required' for url 'https://x'"))
        self.assertIsNotNone(hit)

    def test_timeout_is_not_fatal(self):
        self.assertIsNone(source_fatal_error(TimeoutError("timed out")))

    def test_number_in_url_is_not_a_status(self):
        """URL 里恰好带 402 不能当成状态码，否则会把正常的抓取失败误停用。"""
        self.assertIsNone(source_fatal_error(RuntimeError("fetch https://example.com/402 failed")))

    def test_server_error_is_not_fatal(self):
        """500 是对方挂了，过一会儿可能自己好，不该停用来源。"""
        self.assertIsNone(source_fatal_error(FakeHTTPError(500, "Internal Server Error")))


class SourceAutoDisableTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")
        self.source_id = "src-paywall"
        self.db.execute(
            """INSERT INTO sources(id,type,name,config_json,enabled,poll_seconds,created_at,updated_at)
               VALUES(?,?,?,?,1,300,?,?)""",
            (self.source_id, "wechat_datax", "我的微信公众号", "{}",
             "2026-11-13T00:00:00+00:00", "2026-11-13T00:00:00+00:00"))

    def tearDown(self):
        try:
            self.temp.cleanup()
        except Exception:                       # noqa: BLE001 - Windows 上句柄未放干净不该让测试挂
            pass

    async def test_402_disables_source_instead_of_retrying_forever(self):
        runner = JobRunner(self.db)
        with patch("app.pipeline.poll_source",
                   side_effect=FakeHTTPError(402, "Payment Required")):
            await runner._poll({"id": "job-1"}, {"source_id": self.source_id})

        row = self.db.one("SELECT enabled,error_count,last_error FROM sources WHERE id=?", (self.source_id,))
        self.assertEqual(int(row["enabled"]), 0, "余额不足必须停用，不能每 5 分钟刷一次")
        self.assertIn("已自动停用", row["last_error"])
        self.assertIn("402", row["last_error"])
        events = self.db.query("SELECT type,message FROM events WHERE type='source_disabled'")
        self.assertEqual(len(events), 1)

    async def test_network_error_keeps_source_enabled(self):
        runner = JobRunner(self.db)
        with patch("app.pipeline.poll_source", side_effect=TimeoutError("timed out")):
            with self.assertRaises(TimeoutError):
                await runner._poll({"id": "job-2"}, {"source_id": self.source_id})

        row = self.db.one("SELECT enabled,error_count FROM sources WHERE id=?", (self.source_id,))
        self.assertEqual(int(row["enabled"]), 1, "普通网络错误要留着继续重试")
        self.assertEqual(int(row["error_count"]), 1)
