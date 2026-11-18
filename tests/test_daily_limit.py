"""额度用尽不该被当成「这篇读坏了」。

以前 `_check_daily_limit` 抛的是普通 RuntimeError：worker 按普通失败处理，
重试四次后 `fail_item` 把条目写成 error。结果是当天额度一满，剩下几百篇真
文章全被判死，第二天额度恢复了也没有人再读。这组用例钉住三件事：
抛出的是可识别的 DailyLimitReached、顺延不消耗重试次数、历史误判能被捞回来。
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.services import analyzer
from app.utils import local_date, now_iso

ROOT = Path(__file__).resolve().parents[1]


def _temp_root() -> Path:
    root = ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _settings(limit: int) -> dict:
    return {"llm": {"daily_call_limit": limit}}


class DailyLimitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")
        self.limit = 5
        self.patch = patch.object(analyzer, "get_settings", lambda: _settings(self.limit))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _count(self) -> int:
        row = self.db.one("SELECT value FROM runtime WHERE key=?", ("llm_calls:" + local_date(),))
        return int(row["value"]) if row else 0

    def _set_count(self, value: int) -> None:
        self.db.set_runtime("llm_calls:" + local_date(), str(value))

    def test_under_limit_counts_one_call(self):
        analyzer._check_daily_limit(self.db)
        self.assertEqual(self._count(), 1)

    def test_over_limit_raises_without_consuming(self):
        self._set_count(self.limit)
        with self.assertRaises(analyzer.DailyLimitReached) as ctx:
            analyzer._check_daily_limit(self.db)
        self.assertEqual(self._count(), self.limit)          # 满了就不再计数
        self.assertTrue(ctx.exception.retry_at)               # 带着「什么时候能再排」

    def test_ensure_budget_only_reads(self):
        self._set_count(self.limit - 1)
        analyzer.ensure_daily_budget(self.db)                 # 还有额度，不抛
        self.assertEqual(self._count(), self.limit - 1)       # 也没有偷偷加一次
        self._set_count(self.limit)
        with self.assertRaises(analyzer.DailyLimitReached):
            analyzer.ensure_daily_budget(self.db)

    def test_reset_time_is_in_the_future(self):
        moment = datetime.fromisoformat(analyzer.next_local_reset())
        self.assertGreater(moment, datetime.now().astimezone().replace(microsecond=0))

    def test_parking_keeps_item_alive(self):
        """worker 遇到额度错误：任务顺延、attempts 归零、条目状态不动。"""
        item_id, _ = self.db.discover_item({"url": "https://mp.weixin.qq.com/s?__biz=a&mid=9",
                                            "kind": "article", "title": "一篇真文章",
                                            "author": "某号", "metadata": {}})
        self.db.execute("UPDATE items SET status='discovered' WHERE id=?", (item_id,))
        self.db.enqueue_job("analyze_item", {"version": 1, "item_id": item_id},
                            dedupe_key=f"analyze:{item_id}:initial", priority=60, max_attempts=4)
        job = self.db.claim_job("test-owner")
        self.assertIsNotNone(job)
        exc = analyzer._limit_error(self.limit)
        self.db.reschedule_job(job["id"], exc.retry_at, reason=str(exc), reset_attempts=True)
        parked = self.db.one("SELECT state,attempts,run_at FROM jobs WHERE id=?", (job["id"],))
        self.assertEqual(parked["state"], "RETRY_WAIT")
        self.assertEqual(parked["attempts"], 0)
        self.assertEqual(parked["run_at"][:10], exc.retry_at[:10])
        self.assertEqual(self.db.one("SELECT status FROM items WHERE id=?", (item_id,))["status"],
                         "discovered")


class RequeueLimitBlockedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def _item(self, title: str, error: str) -> str:
        item_id, _ = self.db.discover_item({"url": f"https://mp.weixin.qq.com/s?__biz=a&mid={title}",
                                            "kind": "article", "title": title,
                                            "author": "某号", "metadata": {}})
        self.db.execute("UPDATE items SET status='error',error=? WHERE id=?", (error, item_id))
        return item_id

    def test_only_limit_errors_come_back(self):
        blocked = self._item("被额度挡住的", "analyze_item 多次失败：RuntimeError: 已达本地每日调用保险丝 1200 次；"
                                              "这只限制本项目自己排队，不是 WorkBuddy 的额度")
        broken = self._item("真坏了的", "analyze_item 多次失败：ReadTimeout: 连接超时")
        self.assertEqual(analyzer.requeue_limit_blocked(self.db), 1)
        self.assertEqual(self.db.one("SELECT status,error FROM items WHERE id=?", (blocked,))["status"],
                         "discovered")
        self.assertIsNone(self.db.one("SELECT error FROM items WHERE id=?", (blocked,))["error"])
        self.assertEqual(self.db.one("SELECT status FROM items WHERE id=?", (broken,))["status"], "error")

    def test_running_twice_does_not_duplicate(self):
        self._item("被额度挡住的", f"额度已用完：{now_iso()}")
        self.assertEqual(analyzer.requeue_limit_blocked(self.db), 1)
        self.assertEqual(analyzer.requeue_limit_blocked(self.db), 0)
        jobs = self.db.one("SELECT COUNT(*) c FROM jobs WHERE kind='analyze_item'")["c"]
        self.assertEqual(jobs, 1)

    def test_live_job_blocks_requeue(self):
        item_id = self._item("正在排队的", "RuntimeError: 已达本地每日调用保险丝 1200 次")
        self.db.enqueue_job("analyze_item", {"version": 1, "item_id": item_id},
                            dedupe_key=f"analyze:{item_id}:initial", priority=60)
        self.assertEqual(analyzer.requeue_limit_blocked(self.db), 0)


if __name__ == "__main__":
    unittest.main()
