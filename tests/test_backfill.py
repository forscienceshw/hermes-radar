import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.collectors.sources import poll_wechat_datax
from app.db import Database
from app.utils import now_iso, utcnow

ROOT = Path(__file__).resolve().parents[1]


def article(number: int, days_old: float) -> dict:
    from datetime import timedelta
    return {"id": f"article-{number}", "url": f"https://mp.weixin.qq.com/s/test-{number}",
            "title": f"研究 {number}", "published_at": (utcnow() - timedelta(days=days_old)).isoformat()}


def page(items: list[dict], cursor: str | None, has_more: bool) -> dict:
    return {"data": {"items": items}, "page": {"next_cursor": cursor, "has_more": has_more}}


class BackfillTests(unittest.TestCase):
    def source(self, max_pages: int = 2) -> dict:
        from datetime import timedelta
        return {"id": "source-wechat", "type": "wechat_datax", "name": "测试公众号",
                "config_json": json.dumps({"accounts": [{"id": "gh_1", "name": "科研号"}],
                                           "max_pages_per_account_per_run": max_pages, "overlap_hours": 24,
                                           "initial_backfill_days": 30}),
                "cursor_json": "{}", "last_success_at": (utcnow() - timedelta(days=7)).isoformat()}

    def test_week_offline_pages_until_reaching_previous_checkpoint(self):
        responses = [(page([article(1, 0), article(2, 2)], "cursor-2", True), None),
                     (page([article(3, 6), article(4, 9)], "cursor-3", True), None)]
        with patch("app.collectors.sources._datax_headers", return_value={}), patch("app.collectors.sources.get_json", new=AsyncMock(side_effect=responses)) as mock_get:
            candidates, cursor = asyncio.run(poll_wechat_datax(self.source(), "job-1"))
        self.assertEqual([x["external_key"] for x in candidates], ["article-1", "article-2", "article-3"])
        self.assertFalse(cursor["backfill_pending"])
        self.assertEqual(mock_get.call_count, 2)
        self.assertIsNotNone(cursor["accounts"]["gh_1"]["last_success_at"])

    def test_page_budget_saves_cursor_and_resumes(self):
        source = self.source(max_pages=1)
        with patch("app.collectors.sources._datax_headers", return_value={}), patch("app.collectors.sources.get_json", new=AsyncMock(return_value=(page([article(1, 0)], "cursor-2", True), None))):
            first_candidates, first_cursor = asyncio.run(poll_wechat_datax(source, "job-1"))
        self.assertTrue(first_cursor["backfill_pending"])
        self.assertEqual(first_cursor["accounts"]["gh_1"]["next_cursor"], "cursor-2")
        source["cursor_json"] = json.dumps(first_cursor)
        source["last_success_at"] = now_iso()
        with patch("app.collectors.sources._datax_headers", return_value={}), patch("app.collectors.sources.get_json", new=AsyncMock(return_value=(page([article(4, 9)], None, False), None))) as mock_get:
            second_candidates, second_cursor = asyncio.run(poll_wechat_datax(source, "job-2"))
        self.assertFalse(second_cursor["backfill_pending"])
        self.assertEqual(second_candidates, [])
        self.assertIn("cursor=cursor-2", mock_get.call_args.args[0])

    def test_one_account_failure_does_not_stop_others(self):
        source = self.source()
        source["config_json"] = json.dumps({"accounts": [{"id": "bad", "name": "坏来源"}, {"id": "good", "name": "好来源"}],
                                            "initial_backfill_days": 30})
        effects = [RuntimeError("模拟失败"), (page([article(1, 0)], None, False), None)]
        with patch("app.collectors.sources._datax_headers", return_value={}), patch("app.collectors.sources.get_json", new=AsyncMock(side_effect=effects)):
            candidates, cursor = asyncio.run(poll_wechat_datax(source, "job-1"))
        self.assertEqual(len(candidates), 1)
        self.assertTrue(any("坏来源" in x for x in cursor["warnings"]))

    def test_not_due_account_skips_paid_api_call(self):
        source = self.source()
        source["config_json"] = json.dumps({"accounts": [{"id": "gh_1", "name": "科研号", "interval_seconds": 3600}],
                                            "initial_backfill_days": 30})
        response = (page([article(1, 0)], None, False), None)
        with patch("app.collectors.sources._datax_headers", return_value={}), patch("app.collectors.sources.get_json", new=AsyncMock(return_value=response)):
            _, cursor = asyncio.run(poll_wechat_datax(source, "job-1"))
        self.assertIsNotNone(cursor["accounts"]["gh_1"].get("next_check_at"))
        source["cursor_json"] = json.dumps(cursor)
        with patch("app.collectors.sources._datax_headers", return_value={}), patch("app.collectors.sources.get_json", new=AsyncMock()) as mock_get:
            candidates, _ = asyncio.run(poll_wechat_datax(source, "job-2"))
        self.assertEqual(candidates, [])
        self.assertEqual(mock_get.call_count, 0)

    def test_wecom_customer_service_link_ingest(self):
        from app.collectors.wecom_kf import poll_wecom_kf
        source = {"id": "kf-1", "type": "wecom_kf", "config_json": json.dumps(
            {"corp_id": "ww-test", "open_kf_id": "wk-test", "start_at_now": False}),
            "cursor_json": json.dumps({"sync_cursor": "cursor-1", "prime_complete": True}), "last_success_at": None}
        payload = {"errcode": 0, "errmsg": "ok", "next_cursor": "cursor-2", "has_more": 0, "msg_list": [
            {"msgid": "m1", "origin": 3, "msgtype": "link", "send_time": 1760000000,
             "link": {"title": "Nature论文", "desc": "摘要", "url": "https://mp.weixin.qq.com/s/test?utm_source=x"}},
            {"msgid": "m2", "origin": 3, "msgtype": "text", "send_time": 1760000001,
             "text": {"content": "看看 https://www.bilibili.com/video/BV1dL411u7DW/"}},
            {"msgid": "m3", "origin": 5, "msgtype": "text", "send_time": 1760000002, "text": {"content": "https://ignore.example"}},
        ]}
        with patch("app.collectors.wecom_kf._access_token", new=AsyncMock(return_value="token")), \
             patch("app.collectors.wecom_kf.post_json", new=AsyncMock(return_value=(payload, None))):
            candidates, cursor = asyncio.run(poll_wecom_kf(source, "job"))
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["title"], "Nature论文")
        self.assertEqual(candidates[1]["kind"], "video")
        self.assertEqual(cursor["sync_cursor"], "cursor-2")
        self.assertTrue(cursor["prime_complete"])

    def test_bulk_import_is_idempotent_and_waits_for_key(self):
        from app.api import WeChatImportInput, import_wechat_accounts
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as temp:
            db = Database(Path(temp) / "test.db")
            data = WeChatImportInput(names_text="科研一号\n科研二号\n科研一号\n", initial_backfill_days=30)
            with patch("app.api.db", db), patch("app.api.secret_status", return_value={"datax_api_key": False}):
                first = import_wechat_accounts(data)
                second = import_wechat_accounts(data)
            self.assertTrue(first["needs_key"])
            self.assertEqual(first["added"], 2)
            self.assertEqual(second["added"], 0)
            self.assertEqual(db.one("SELECT COUNT(*) c FROM sources")["c"], 1)
            accounts = json.loads(db.one("SELECT config_json FROM sources")["config_json"])["accounts"]
            self.assertEqual(len(accounts), 2)
            self.assertEqual(db.one("SELECT enabled FROM sources")["enabled"], 0)


if __name__ == "__main__":
    unittest.main()
