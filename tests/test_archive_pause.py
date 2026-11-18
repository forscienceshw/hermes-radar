"""批量归档要能停得住，而且停得干净。

用户按「停止」时，worker 里那一批可能正卡在一次 HTTP 下载里。硬杀协程会把条目
留在半截状态，所以做法是「标记 + 排空」：置 runtime 标记 → 取消还没领走的批次
任务 → 正在跑的那一批在条目之间检查标记，做完手上这条就退出，不再续排。

这组用例钉三件事：暂停期间重排进来的批次直接空转退出；跑到一半置标记只做完当前
这一条；没暂停时批次照常续排（不能因为加了开关就永远停住）。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.pipeline import JobRunner
from app.utils import ARCHIVE_PAUSE_KEY, now_iso

ROOT = Path(__file__).resolve().parents[1]


def _temp_root() -> Path:
    root = ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


class ArchivePauseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")
        self.runner = JobRunner(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _add_items(self, count: int) -> list[str]:
        ids = []
        for i in range(count):
            item_id = f"item-{i}"
            self.db.execute(
                """INSERT INTO items(id,kind,external_key,url,canonical_url,title,discovered_at,status)
                   VALUES(?,?,?,?,?,?,?, 'discovered')""",
                (item_id, "wechat_public", f"k{i}", f"https://e/{i}", f"https://e/{i}",
                 f"标题{i}", now_iso()))
            ids.append(item_id)
        return ids

    def _pending_batches(self) -> int:
        return self.db.one("""SELECT COUNT(*) c FROM jobs WHERE kind='archive_history_batch'
            AND state IN ('READY','RETRY_WAIT','LEASED')""")["c"]

    async def test_paused_batch_exits_without_touching_items(self):
        self._add_items(3)
        self.db.set_runtime(ARCHIVE_PAUSE_KEY, now_iso())
        calls = []

        async def fake_archive(item_id, archive_only=False):
            calls.append(item_id)

        self.runner._archive = fake_archive
        await self.runner._archive_history_batch({"version": 1, "batch_size": 8})

        self.assertEqual(calls, [], "暂停期间不应该动任何条目")
        self.assertEqual(self._pending_batches(), 0, "暂停的批次不该再续排下一批")

    async def test_pause_mid_batch_stops_after_current_item(self):
        self._add_items(5)
        calls = []

        async def fake_archive(item_id, archive_only=False):
            calls.append(item_id)
            # 模拟用户在第一篇归档途中按了停止
            self.db.set_runtime(ARCHIVE_PAUSE_KEY, now_iso())

        self.runner._archive = fake_archive
        await self.runner._archive_history_batch({"version": 1, "batch_size": 8})

        self.assertEqual(len(calls), 1, "按停止后只应把手上这一条做完")
        self.assertEqual(self._pending_batches(), 0, "停止后不应该续排下一批")
        # 桩函数没有真的归档，所以五条都还在 discovered；关键是另外四条一根手指都没碰。
        left = self.db.one("SELECT COUNT(*) c FROM items WHERE status='discovered'")["c"]
        self.assertEqual(left, 5, "没处理的条目要原样留在 discovered 里")

    async def test_running_batch_keeps_chaining_when_not_paused(self):
        self._add_items(3)
        calls = []

        async def fake_archive(item_id, archive_only=False):
            calls.append(item_id)

        self.runner._archive = fake_archive
        await self.runner._archive_history_batch({"version": 1, "batch_size": 8})

        self.assertEqual(len(calls), 3)
        self.assertEqual(self._pending_batches(), 1, "还有剩余条目时必须续排下一批")

    async def test_failed_items_do_not_stop_the_batch(self):
        self._add_items(3)
        calls = []

        async def fake_archive(item_id, archive_only=False):
            calls.append(item_id)
            raise RuntimeError("模拟下载失败")

        self.runner._archive = fake_archive
        await self.runner._archive_history_batch({"version": 1, "batch_size": 8})

        self.assertEqual(len(calls), 3, "单条失败不能中断整批")
        # 三条都失败后被标成 error，队列里没有待归档条目，自然也不用再排下一批。
        failed = self.db.one("SELECT COUNT(*) c FROM items WHERE status='error'")["c"]
        self.assertEqual(failed, 3)
        self.assertEqual(self._pending_batches(), 0)

    def test_stop_endpoint_cancels_queued_batches(self):
        self._add_items(2)
        self.db.enqueue_job("archive_history_batch", {"version": 1, "batch_size": 8},
                            dedupe_key="t1", priority=200)
        self.db.enqueue_job("archive_history_batch", {"version": 1, "batch_size": 8},
                            dedupe_key="t2", priority=200)
        self.assertEqual(self._pending_batches(), 2)

        from app.pipeline import stop_archive_batch
        result = stop_archive_batch(self.db)

        self.assertTrue(result["paused"])
        self.assertEqual(result["cancelled"], 2)
        self.assertEqual(self._pending_batches(), 0)
        self.assertTrue(self.db.get_runtime(ARCHIVE_PAUSE_KEY))

    def test_start_clears_pause(self):
        self._add_items(1)
        self.db.set_runtime(ARCHIVE_PAUSE_KEY, now_iso())

        from app.pipeline import start_archive_batch
        start_archive_batch(self.db)

        self.assertFalse(self.db.get_runtime(ARCHIVE_PAUSE_KEY), "手动开始归档必须清掉暂停标记")
        self.assertEqual(self._pending_batches(), 1)


if __name__ == "__main__":
    unittest.main()
