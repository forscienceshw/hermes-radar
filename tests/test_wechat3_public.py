"""3.x 公众号库（PublicMsg.db）离线通道的回归测试。

这条路的失败模式和 4.x 不一样：4.x 是"读到了但库是空的"，这条路是"解不开"
或"解开了但把不是文章的东西当文章"。所以钉三件事：密钥校验、卡片解析、
以及游标一定要往前走（不推进就会每轮重扫同一千条，看着像在工作其实是死循环）。
"""
from __future__ import annotations

import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.services import wechat3_public as w3


def _make_public_db(path: Path, rows: list[tuple]) -> Path:
    """造一个明文的 PublicMsg 库，只保留这条通道真正会读的字段。"""
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE PublicMsg(
        localId INTEGER PRIMARY KEY, TalkerId INTEGER, MsgSvrID INTEGER,
        Type INTEGER, SubType INTEGER, IsSender INTEGER, CreateTime INTEGER,
        StrTalker TEXT, StrContent TEXT, DisplayContent TEXT, CompressContent BLOB)""")
    con.executemany(
        "INSERT INTO PublicMsg(localId,Type,SubType,CreateTime,StrTalker,StrContent,CompressContent)"
        " VALUES(?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def _appmsg(title: str, url: str, account: str = "某号", digest: str = "摘要") -> bytes:
    xml = (f"<msg><appmsg><title>{title}</title><des>{digest}</des>"
           f"<url>{url}</url><sourcedisplayname>{account}</sourcedisplayname>"
           f"</appmsg></msg>")
    import lz4.block
    return lz4.block.compress(xml.encode("utf-8"))


class CardTests(unittest.TestCase):
    def test_parses_lz4_appmsg(self) -> None:
        got = w3.card(_appmsg("标题甲", "https://mp.weixin.qq.com/s/abc"), "")
        self.assertEqual(got["title"], "标题甲")
        self.assertEqual(got["url"], "https://mp.weixin.qq.com/s/abc")
        self.assertEqual(got["account"], "某号")

    def test_falls_back_to_plain_xml(self) -> None:
        xml = "<msg><appmsg><title>纯文本</title><url>https://mp.weixin.qq.com/s/x</url></appmsg></msg>"
        self.assertEqual(w3.card(None, xml)["title"], "纯文本")

    def test_junk_is_not_a_card(self) -> None:
        self.assertEqual(w3.card(b"\x00\x01\x02", "not xml"), {})
        self.assertEqual(w3.card(None, ""), {})


class ArticleQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "PublicMsg.db"
        now = int(time.time())
        _make_public_db(self.db, [
            (1, 49, 5, now - 300, "gh_aaa",
             _appmsg("文章一", "https://mp.weixin.qq.com/s/one").decode("latin-1") if False else "",
             _appmsg("文章一", "https://mp.weixin.qq.com/s/one")),
            (2, 49, 5, now - 200, "gh_bbb", "",
             _appmsg("非微信链接", "https://example.com/x")),
            (3, 1, 0, now - 100, "gh_aaa", "普通文字消息", None),
            (4, 49, 5, now - 50, "gh_aaa", "",
             _appmsg("文章二", "https://mp.weixin.qq.com/s/two")),
        ])
        self.snap = {"db": self.db, "source": self.db, "cached": True, "pages": 0}

    def test_keeps_only_mp_weixin_links(self) -> None:
        rows = w3.articles(after_local_id=0, limit=100, snap=self.snap)
        self.assertEqual([row["local_id"] for row in rows], [1, 4])
        self.assertEqual(rows[0]["title"], "文章一")

    def test_cursor_advances_by_local_id(self) -> None:
        rows = w3.articles(after_local_id=1, limit=100, snap=self.snap)
        self.assertEqual([row["local_id"] for row in rows], [4])

    def test_since_filters_out_old_rows(self) -> None:
        rows = w3.articles(since_ts=int(time.time()) - 120, after_local_id=0,
                           limit=100, snap=self.snap)
        self.assertEqual([row["local_id"] for row in rows], [4])

    def test_watermark_reads_max_local_id(self) -> None:
        self.assertEqual(w3.watermark(self.snap), 4)


class SnapshotTests(unittest.TestCase):
    def test_missing_database_returns_none(self) -> None:
        with mock.patch.object(w3, "pick", return_value=None):
            self.assertIsNone(w3.snapshot())

    def test_no_key_returns_none_instead_of_raising(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "PublicMsg.db"
            db.write_bytes(b"\x00" * 8192)
            with mock.patch.object(w3, "pick", return_value={"path": db, "size": 8192, "mtime": 0}):
                self.assertIsNone(w3.snapshot(key_hex=""))
                self.assertIsNone(w3.snapshot(key_hex="0" * 64))

    def test_key_ok_rejects_wrong_key(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "PublicMsg.db"
            db.write_bytes(b"\x11" * 8192)
            self.assertFalse(w3.key_ok(db, "a" * 64))
            self.assertFalse(w3.key_ok(db, ""))


class FingerprintTests(unittest.TestCase):
    def test_changes_when_file_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "PublicMsg.db"
            db.write_bytes(b"a" * 4096)
            first = w3.fingerprint(db)
            db.write_bytes(b"a" * 8192)
            self.assertNotEqual(first, w3.fingerprint(db))

    def test_wal_participates(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "PublicMsg.db"
            db.write_bytes(b"a" * 4096)
            first = w3.fingerprint(db)
            Path(str(db) + "-wal").write_bytes(b"b" * 100)
            self.assertNotEqual(first, w3.fingerprint(db))


class PickTests(unittest.TestCase):
    def test_prefers_newest_database(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "wxid_old" / "Msg"
            new = root / "wxid_new" / "Msg"
            for folder in (old, new):
                folder.mkdir(parents=True)
                (folder / "PublicMsg.db").write_bytes(b"x" * 8192)
            import os
            os.utime(old / "PublicMsg.db", (1_600_000_000, 1_600_000_000))
            os.utime(new / "PublicMsg.db", (1_700_000_000, 1_700_000_000))
            with mock.patch.object(w3, "roots", return_value=[root]):
                chosen = w3.pick()
        self.assertEqual(chosen["path"].parent.parent.name, "wxid_new")


class HistoryCursorTests(unittest.TestCase):
    """4.x 分支里那条 3.x 历史通道：要能补、要会推进、不许越权。"""

    def setUp(self) -> None:
        from app.services import wechat_self_archive as archive
        self.archive = archive
        self.source = {"id": "src-1"}
        self.rows = [
            {"user_name": "gh_aaa", "local_id": 10, "create_time": 1_700_000_000,
             "title": "甲", "digest": "", "url": "https://mp.weixin.qq.com/s/a", "account": "号A"},
            {"user_name": "gh_zzz", "local_id": 11, "create_time": 1_700_000_001,
             "title": "乙", "digest": "", "url": "https://mp.weixin.qq.com/s/b", "account": "号Z"},
        ]

    def test_first_run_backfills_and_filters(self) -> None:
        with mock.patch.object(w3, "snapshot", return_value={"db": Path("x"), "source": Path("x"), "cached": True}), \
             mock.patch.object(w3, "articles", return_value=self.rows) as query:
            candidates, cursor = self.archive._wechat3_history_candidates(
                self.source, {"initial_backfill_days": 7}, {},
                {"gh_aaa"}, set(), {"gh_aaa": "号A"}, True)
        self.assertEqual([item["url"] for item in candidates], ["https://mp.weixin.qq.com/s/a"])
        self.assertEqual(cursor["local_id"], 11, "被过滤掉的号也要推进游标，否则每轮重扫")
        self.assertEqual(cursor["scanned"], 2)
        self.assertEqual(cursor["backfill_days"], 7)
        self.assertEqual(query.call_args.kwargs["since_ts"] is not None, True)

    def test_resume_uses_cursor_without_new_window(self) -> None:
        with mock.patch.object(w3, "snapshot", return_value={"db": Path("x"), "source": Path("x"), "cached": True}), \
             mock.patch.object(w3, "articles", return_value=[]) as query:
            _, cursor = self.archive._wechat3_history_candidates(
                self.source, {}, {"wechat3": {"local_id": 900}}, set(), set(), {}, False)
        self.assertEqual(query.call_args.kwargs["after_local_id"], 900)
        self.assertIsNone(query.call_args.kwargs["since_ts"])
        self.assertEqual(cursor["local_id"], 900, "没新数据时游标不能倒退")

    def test_excluded_account_is_dropped(self) -> None:
        with mock.patch.object(w3, "snapshot", return_value={"db": Path("x"), "source": Path("x"), "cached": True}), \
             mock.patch.object(w3, "articles", return_value=self.rows):
            candidates, _ = self.archive._wechat3_history_candidates(
                self.source, {}, {}, {"gh_aaa"}, {"gh_aaa"}, {}, True)
        self.assertEqual(candidates, [])

    def test_unavailable_channel_is_reported_not_raised(self) -> None:
        with mock.patch.object(w3, "snapshot", return_value=None):
            candidates, cursor = self.archive._wechat3_history_candidates(
                self.source, {}, {}, set(), set(), {}, False)
        self.assertEqual(candidates, [])
        self.assertFalse(cursor["available"])
        self.assertTrue(cursor["reason"])

    def test_snapshot_error_does_not_break_polling(self) -> None:
        with mock.patch.object(w3, "snapshot", side_effect=RuntimeError("boom")):
            candidates, cursor = self.archive._wechat3_history_candidates(
                self.source, {}, {}, set(), set(), {}, False)
        self.assertEqual(candidates, [])
        self.assertIn("boom", cursor["reason"])


if __name__ == "__main__":          # pragma: no cover
    unittest.main()
