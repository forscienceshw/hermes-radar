"""精读并发：能同时跑几篇，以及额度调高后顺延任务要能立刻回来。

两件事必须钉住：

1. 精读排满时只能跳过 `analyze_item`，轮询和归档还得照常出队——否则把并发
   设成 1 就等于把整个 worker 变成单线程，30 秒扫号会被一篇长文拖死。
2. 用户在设置里把每日上限调高以后，已经顺延到明天的任务要自动提前回来；
   只看额度不看 run_at 的话，用户会以为"改了设置没反应"。
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.db import Database
from app.services import analyzer
from app.worker import Worker

ROOT = Path(__file__).resolve().parents[1]


def _temp_root() -> Path:
    root = ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _later(hours: int = 12) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="milliseconds")


class ClaimExcludeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_skip_analyze_but_still_claim_poll(self):
        self.db.enqueue_job("analyze_item", {"version": 1, "item_id": "a"}, priority=10,
                            dedupe_key="analyze:a")
        self.db.enqueue_job("poll_source", {"version": 1, "source_id": "s"}, priority=90,
                            dedupe_key="poll:s")
        job = self.db.claim_job("owner", exclude_kinds=("analyze_item",))
        self.assertEqual(job["kind"], "poll_source")
        job = self.db.claim_job("owner", exclude_kinds=("analyze_item",))
        self.assertIsNone(job)                       # 只剩精读，这一轮就不领了

    def test_without_exclusion_priority_wins(self):
        self.db.enqueue_job("poll_source", {"version": 1, "source_id": "s"}, priority=90,
                            dedupe_key="poll:s")
        self.db.enqueue_job("analyze_item", {"version": 1, "item_id": "a"}, priority=10,
                            dedupe_key="analyze:a")
        self.assertEqual(self.db.claim_job("owner")["kind"], "analyze_item")


class BudgetResumeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")
        self.limit = 5
        self.patch = patch.object(analyzer, "get_settings",
                                  lambda: {"llm": {"daily_call_limit": self.limit}})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _parked(self, item_id: str, error: str, hours: int = 12) -> str:
        self.db.enqueue_job("analyze_item", {"version": 1, "item_id": item_id}, priority=60,
                            dedupe_key=f"analyze:{item_id}")
        job = self.db.one("SELECT id FROM jobs WHERE json_extract(payload_json,'$.item_id')=?",
                          (item_id,))
        self.db.execute("""UPDATE jobs SET state='RETRY_WAIT',run_at=?,attempts=0,last_error=?
            WHERE id=?""", (_later(hours), error, job["id"]))
        return job["id"]

    def test_budget_left_counts_remaining(self):
        self.assertEqual(analyzer.daily_budget_left(self.db), self.limit)
        self.db.set_runtime("llm_calls:" + analyzer.local_date(), str(self.limit - 2))
        self.assertEqual(analyzer.daily_budget_left(self.db), 2)

    def test_parked_job_returns_when_limit_raised(self):
        job_id = self._parked("a", "今天的 5 次本地精读额度已用完，剩余条目自动顺延到明天，不占重试次数")
        self.db.set_runtime("llm_calls:" + analyzer.local_date(), str(self.limit))
        self.assertEqual(analyzer.resume_budget_parked(self.db), 0)   # 没额度，别放
        self.db.set_runtime("llm_calls:" + analyzer.local_date(), "0")
        self.assertEqual(analyzer.resume_budget_parked(self.db), 1)   # 调高了，放回来
        row = self.db.one("SELECT state,run_at,last_error FROM jobs WHERE id=?", (job_id,))
        self.assertEqual(row["state"], "READY")
        self.assertLessEqual(row["run_at"], datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        self.assertIsNone(row["last_error"])

    def test_other_delays_are_left_alone(self):
        self._parked("b", "微信验证 #1：需要人机验证")
        self.assertEqual(analyzer.resume_budget_parked(self.db), 0)
        self.assertEqual(self.db.one("SELECT state FROM jobs")["state"], "RETRY_WAIT")

    def test_resume_is_batched(self):
        for index in range(5):
            self._parked(f"i{index}", "今天的 5 次本地精读额度已用完")
        self.assertEqual(analyzer.resume_budget_parked(self.db, batch=2), 2)
        self.assertEqual(analyzer.resume_budget_parked(self.db, batch=2), 2)


class WorkerLimitTest(unittest.TestCase):
    def tearDown(self):
        patch.stopall()

    def test_limit_is_clamped(self):
        for raw, expected in ((0, 1), (3, 3), (8, 8), (99, 8), ("坏值", 3), (None, 3)):
            with patch("app.worker.get_settings", lambda raw=raw: {"analysis": {"concurrency": raw}}):
                self.assertEqual(Worker._read_analysis_limit(), expected, raw)

    def test_skip_only_when_full(self):
        worker = SimpleNamespace(_analyze_running=2, analysis_limit=3)
        self.assertEqual(Worker._skip_kinds(worker), ())
        worker._analyze_running = 3
        self.assertEqual(Worker._skip_kinds(worker), ("analyze_item",))

    def test_loop_count_covers_analysis_plus_one(self):
        with patch("app.worker.get_settings", lambda: {"worker_concurrency": 3}):
            worker = SimpleNamespace(analysis_limit=5)
            self.assertEqual(Worker._desired_loops(worker), 6)     # 5 精读 + 1 杂活
            worker.analysis_limit = 1
            self.assertEqual(Worker._desired_loops(worker), 3)     # 至少保持原来的 3


if __name__ == "__main__":
    unittest.main()
