"""每日快照的回归测试。

重点是两条：连接要真的关掉（否则 Windows 上改名失败，快照永远停在 `.part`），
以及滚动删除只碰"每日快照"，`radar-before-*` 这种手动留底永远不动。
"""
from __future__ import annotations

import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.services import backup


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "radar.db"
        with sqlite3.connect(str(self.db_path)) as con:
            con.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT)")
            con.execute("INSERT INTO items(title) VALUES('x')")
        self.backup_dir = self.root / "backups"
        self.patch = mock.patch.multiple(backup, DB_PATH=self.db_path, BACKUP_DIR=self.backup_dir)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        try:
            self.temp.cleanup()
        except BaseException:            # noqa: BLE001 - 垫片可能拒绝 rmtree
            pass

    def test_daily_snapshot_is_complete_and_not_left_partial(self) -> None:
        result = backup.run(force=True)
        self.assertTrue(result["ok"], result.get("error"))
        target = Path(result["created"])
        self.assertTrue(target.is_file())
        self.assertFalse((self.backup_dir / (target.name + ".part")).exists())
        # 快照本身要是能打开的完整库
        with sqlite3.connect(str(target)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    def test_second_run_same_day_is_skipped(self) -> None:
        first = backup.run(force=True)
        second = backup.run()
        self.assertTrue(second.get("skipped"))
        self.assertEqual(first["created"], second["target"])

    def test_retention_only_touches_daily_snapshots(self) -> None:
        # 造 9 份"每天"的快照 + 1 份手动留底
        for day in range(1, 10):
            path = self.backup_dir / f"radar-2026110{day}.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            stamp = time.time() - (10 - day) * 86400
            import os
            os.utime(path, (stamp, stamp))
        manual = self.backup_dir / "radar-before-rollback-20261114.db"
        manual.write_bytes(b"keep me")

        result = backup.run(force=True)
        self.assertTrue(result["ok"], result.get("error"))
        remaining = sorted(path.name for path in backup.backups())
        self.assertEqual(len(remaining), backup.keep_count())
        self.assertTrue(manual.is_file(), "手动留底被滚动删除误伤了")

    def test_retention_follows_setting(self) -> None:
        """保留份数由 settings.storage.backup_keep 决定，不再写死 7。"""
        for day in range(1, 7):
            path = self.backup_dir / f"radar-2026110{day}.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            stamp = time.time() - (10 - day) * 86400
            import os
            os.utime(path, (stamp, stamp))
        with mock.patch.object(backup, "get_settings",
                               return_value={"storage": {"backup_keep": 2}}):
            self.assertEqual(backup.keep_count(), 2)
            result = backup.run(force=True)
        self.assertTrue(result["ok"], result.get("error"))
        # 6 份旧的 + 今天这份 = 7，只留最近 2 份
        self.assertEqual(len(backup.backups()), 2)

    def test_broken_keep_setting_falls_back(self) -> None:
        for value in ("abc", None, -3, 0, 999):
            with mock.patch.object(backup, "get_settings",
                                   return_value={"storage": {"backup_keep": value}}):
                self.assertGreaterEqual(backup.keep_count(), 1, value)


if __name__ == "__main__":
    unittest.main()
