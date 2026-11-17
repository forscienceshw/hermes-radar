"""4.x 通道分派、分片读取、补漏窗口的回归测试。

这三个地方任何一个错，表现都一样：体检是绿的，推文一篇都不进来。所以宁可
把它们钉死。
"""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.services import wechat4_live as live
from app.services import wechat_self_archive as archive


class ShardListingTests(unittest.TestCase):
    def test_expands_shards_newest_first(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "message"
            root.mkdir()
            for index in (0, 2, 1, 10):
                (root / f"biz_message_{index}.db").write_bytes(b"x")
            (root / "biz_message_0.db-wal").write_bytes(b"x")
            names = [path.name for path in live.message_dbs(Path(tmp), "message/biz_message_*.db")]
        # 按编号升序：编号越小越新
        self.assertEqual(names, ["biz_message_0.db", "biz_message_1.db",
                                 "biz_message_2.db", "biz_message_10.db"])

    def test_plain_path_still_works(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "one.db"
            target.write_bytes(b"x")
            self.assertEqual(live.message_dbs(Path(tmp), "one.db"), [target])
            self.assertEqual(live.message_dbs(Path(tmp), "missing.db"), [])


class WindowTests(unittest.TestCase):
    def test_first_run_uses_initial_backfill(self) -> None:
        since = archive._wechat4_window({}, {"initial_backfill_days": 7})
        self.assertAlmostEqual(since, time.time() - 7 * 86400, delta=30)

    def test_resumes_from_cursor(self) -> None:
        cursor = {"last_create_time": int(time.time() - 3600)}
        since = archive._wechat4_window(cursor, {})
        self.assertAlmostEqual(since, time.time() - 3600, delta=30)

    def test_long_offline_is_capped_by_catchup_days(self) -> None:
        cursor = {"last_create_time": int(time.time() - 100 * 86400)}
        since = archive._wechat4_window(cursor, {"catchup_days": 30})
        self.assertAlmostEqual(since, time.time() - 30 * 86400, delta=60)

    def test_future_cursor_does_not_eat_everything(self) -> None:
        cursor = {"last_create_time": int(time.time() + 86400)}
        since = archive._wechat4_window(cursor, {})
        self.assertLessEqual(since, int(time.time()))
        self.assertGreater(since, int(time.time()) - 3600)

    def test_bad_cursor_falls_back_to_initial(self) -> None:
        since = archive._wechat4_window({"last_create_time": "坏值"}, {"initial_backfill_days": 3})
        self.assertAlmostEqual(since, time.time() - 3 * 86400, delta=30)


class ChannelTests(unittest.TestCase):
    def _pick(self, four_ok: bool, running: bool, three_ok: bool,
              four_at: float = 0.0, three_at: float = 0.0) -> str:
        with mock.patch.object(archive, "_wechat3_present", return_value=three_ok), \
             mock.patch.object(archive, "_wechat4_freshness", return_value=four_at), \
             mock.patch.object(archive, "_wechat3_freshness", return_value=three_at), \
             mock.patch("app.services.wechat4_live.available", return_value=four_ok), \
             mock.patch("app.services.wechat4_live.quick_status",
                        return_value={"running": running, "storages": ["x"], "databases": 1}):
            return archive.local_channel()

    def test_running_4x_wins_even_with_3x_folder(self) -> None:
        # 升级后 3.x 目录还在（还常是软链接），不能因此继续读一份冻住的库
        self.assertEqual(self._pick(four_ok=True, running=True, three_ok=True,
                                    four_at=100, three_at=999), "4.x")

    def test_only_3x_uses_3x(self) -> None:
        self.assertEqual(self._pick(four_ok=False, running=False, three_ok=True), "3.x")

    def test_only_4x_uses_4x(self) -> None:
        self.assertEqual(self._pick(four_ok=True, running=False, three_ok=False), "4.x")

    def test_both_quiet_picks_newer_database(self) -> None:
        self.assertEqual(self._pick(four_ok=True, running=False, three_ok=True,
                                    four_at=10, three_at=20), "3.x")
        self.assertEqual(self._pick(four_ok=True, running=False, three_ok=True,
                                    four_at=20, three_at=10), "4.x")

    def test_nothing_available(self) -> None:
        self.assertEqual(self._pick(four_ok=False, running=False, three_ok=False), "")


class DiagnoseTests(unittest.TestCase):
    def test_counts_article_cards_across_shards(self) -> None:
        from app.services import wechat_space  # noqa: F401 - 只确认模块能导入

        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "message").mkdir()
            (cache / "keys.json").write_text("{}", encoding="utf-8")
            status = {"running": False, "storages": [], "databases": 0}
            with mock.patch.object(live, "quick_status", return_value=status), \
                 mock.patch.object(live, "DEFAULT_CACHE", cache), \
                 mock.patch.object(live, "_connect", side_effect=Exception("no db")):
                result = live.diagnose(days=7)
        self.assertEqual(result["channel"], "4.x")
        self.assertEqual(result["articles"], 0)
        self.assertEqual(result["shards"], [])


class PushSummaryTests(unittest.TestCase):
    """会话列表和正文必须分开报，否则"读到 0 篇"分不清是谁的问题。"""

    def _cache(self, root: Path) -> Path:
        import sqlite3
        cache = root / "wechat4"
        (cache / "session").mkdir(parents=True)
        (cache / "contact").mkdir(parents=True)
        now = int(time.time())
        con = sqlite3.connect(str(cache / "session" / "session.db"))
        con.execute("CREATE TABLE SessionTable (username, last_timestamp, summary)")
        con.executemany("INSERT INTO SessionTable VALUES (?,?,?)", [
            ("gh_recent", now - 600, "刚推的一篇"),
            ("gh_week", now - 3 * 86400, "三天前那篇"),
            ("gh_old", now - 40 * 86400, "早过期了"),
            ("wxid_friend", now - 60, "私聊不算公众号"),
        ])
        con.commit()
        con.close()
        con = sqlite3.connect(str(cache / "contact" / "contact.db"))
        con.execute("CREATE TABLE contact (username, remark, nick_name, alias)")
        con.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
            ("gh_recent", "", "生物谷", ""),
            ("gh_week", "", "BioArt", ""),
            ("gh_old", "", "老号", ""),
        ])
        con.commit()
        con.close()
        return cache

    def test_counts_recent_pushes_and_names_them(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp))
            result = live.push_summary(days=7, cache=cache)
        # 只数 gh_，40 天前那个和私聊都不算
        self.assertEqual(result["accounts"], 2)
        self.assertEqual(result["latest_account"], "生物谷")
        self.assertEqual([item["account"] for item in result["samples"]], ["生物谷", "BioArt"])

    def test_missing_session_db_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            result = live.push_summary(days=7, cache=Path(tmp) / "nothing")
        self.assertEqual(result["accounts"], 0)
        self.assertEqual(result["samples"], [])


class SessionActivityTests(unittest.TestCase):
    """会话表是唯一能证明「客户端还在不在收公众号」的地方，必须钉住。"""

    def _cache(self, root: Path, last_gh: int, last_chat: int) -> Path:
        import sqlite3
        cache = root / "wechat4"
        (cache / "session").mkdir(parents=True)
        (cache / "contact").mkdir(parents=True)
        con = sqlite3.connect(str(cache / "session" / "session.db"))
        con.execute("CREATE TABLE SessionTable (username, last_timestamp, unread_count, "
                    "summary, last_msg_type)")
        con.executemany("INSERT INTO SessionTable VALUES (?,?,?,?,?)", [
            ("gh_zhuyun", last_gh, 2, "知云那篇推文", 49),
            ("gh_bio", last_gh - 3600, 1, "另一家的推文", 49),
            ("gh_old", last_gh - 40 * 86400, 1, "很久以前", 49),
            ("wxid_friend", last_chat, 0, "私聊", 1),
        ])
        con.commit()
        con.close()
        con = sqlite3.connect(str(cache / "contact" / "contact.db"))
        con.execute("CREATE TABLE contact (username, remark, nick_name, alias)")
        con.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
            ("gh_zhuyun", "", "知云文献阅读", ""),
            ("gh_bio", "", "文献速递", ""),
            ("gh_old", "", "老号", ""),
        ])
        con.commit()
        con.close()
        return cache

    def test_counts_only_gh_and_reports_gap(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), last_gh=now - 4 * 3600, last_chat=now - 60)
            result = live.session_activity(hours=3, cache=cache)
        self.assertEqual(result["accounts"], 3)
        self.assertEqual(result["recent"], 0)          # 4 小时前那次不在 3 小时窗口里
        self.assertAlmostEqual(result["last_ago"], 4.0, delta=0.2)
        self.assertEqual(result["last_display"], time.strftime(
            "%m-%d %H:%M", time.localtime(now - 4 * 3600)))
        # 私聊还在收 → 证明客户端活着，只是没在收公众号
        self.assertGreater(result["chat_at"], result["last_at"])

    def test_missing_session_db_is_empty_not_crash(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(live.session_activity(cache=Path(tmp) / "nope"), {})
            self.assertEqual(live.session_hint("gh_x", cache=Path(tmp) / "nope"), {})

    def test_hint_keeps_title_when_body_missing(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), last_gh=now - 600, last_chat=now - 60)
            hint = live.session_hint("gh_zhuyun", cache=cache)
        self.assertEqual(hint["title"], "知云那篇推文")
        self.assertEqual(hint["unread"], 2)
        self.assertEqual(hint["last_type"], 49)

    def test_name_to_gh_must_be_unique(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), last_gh=1, last_chat=1)
            self.assertEqual(live.gh_for_name("知云文献阅读", cache=cache), "gh_zhuyun")
            # 「文献」同时命中「知云文献阅读」，不唯一就不能瞎认
            self.assertEqual(live.gh_for_name("文献", cache=cache), "")
            self.assertEqual(live.gh_for_name("不存在的号", cache=cache), "")


class HolderSessionTests(unittest.TestCase):
    """订阅号聚合会话是 4.x 唯一带标题的线索，漏掉它就会误判成"通道停了"。"""

    def _cache(self, root: Path, holder_at: int, gh_at: int, local_id: int) -> Path:
        import sqlite3
        cache = root / "wechat4"
        (cache / "session").mkdir(parents=True)
        con = sqlite3.connect(str(cache / "session" / "session.db"))
        con.execute("CREATE TABLE SessionTable (username, last_timestamp, unread_count, "
                    "summary, last_msg_type, last_msg_sender, last_sender_display_name, "
                    "last_msg_locald_id)")
        con.executemany("INSERT INTO SessionTable VALUES (?,?,?,?,?,?,?,?)", [
            ("brandsessionholder", holder_at, 2,
             "一天三篇 Cell！任兵团队绘制大脑调控地图", 49, "gh_bio", "BioGOGO", local_id),
            ("brandservicesessionholder", gh_at - 7200, 0, "知云开了个班", 49,
             "gh_zhuyun", "知云文献阅读", 2),
            ("gh_zhuyun", gh_at, 1, "", 49, "", "", 2),
            ("wxid_friend", gh_at - 60, 0, "私聊", 1, "", "", 1),
        ])
        con.commit()
        con.close()
        return cache

    def test_holder_counts_as_activity_and_reports_sender(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), holder_at=now - 600, gh_at=now - 4 * 3600,
                                local_id=0)
            result = live.session_activity(hours=3, cache=cache)
        # gh_ 都停在 4 小时前，但聚合会话 10 分钟前刚动过 —— 这就是"通道还活着"
        self.assertEqual(result["recent"], 1)
        self.assertAlmostEqual(result["last_ago"], 0.2, delta=0.1)
        self.assertIn("任兵", result["last_title"])
        self.assertEqual(result["last_sender_name"], "BioGOGO")
        self.assertEqual(result["last_sender"], "gh_bio")
        self.assertFalse(result["stored"])             # local_id=0 → 正文没落盘

    def test_stored_true_when_local_id_present(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), holder_at=now - 600, gh_at=now - 60, local_id=7)
            self.assertTrue(live.session_activity(hours=3, cache=cache)["stored"])

    def test_holder_does_not_pollute_chat_time(self) -> None:
        """私聊时间必须只看真会话，不能把订阅号聚合算成"私聊还在收"。"""
        now = int(time.time())
        with TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), holder_at=now - 60, gh_at=now - 3600,
                                local_id=0)
            result = live.session_activity(hours=3, cache=cache)
        self.assertLess(result["chat_at"], result["last_at"])


class FingerprintTests(unittest.TestCase):
    """worker 靠这个指纹判断"微信刚写了公众号"，误报会白跑一轮。"""

    def test_ignores_shm_and_dedupes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "message"
            folder.mkdir()
            for name in ("biz_message_0.db", "biz_message_0.db-wal",
                         "biz_message_0.db-shm", "biz_message_0.db-first.material",
                         "message_0.db"):
                (folder / name).write_bytes(b"x" * 10)
            marks = live.biz_fingerprint(storage=root)
        names = [m[0] for m in marks]
        self.assertEqual(names, ["biz_message_0.db", "biz_message_0.db-wal"])

    def test_grows_when_a_write_happens(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "message"
            folder.mkdir()
            target = folder / "biz_message_1.db"
            target.write_bytes(b"a" * 10)
            before = live.biz_fingerprint(storage=root)
            target.write_bytes(b"a" * 40)
            after = live.biz_fingerprint(storage=root)
        self.assertNotEqual(before, after)

    def test_missing_folder_is_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(live.biz_fingerprint(storage=Path(tmp)), ())


class SpaceTests(unittest.TestCase):
    def test_on_c_only_matches_system_drive(self) -> None:
        from app.services import wechat_space
        self.assertTrue(wechat_space._on_c("C:\\Users\\x"))
        self.assertTrue(wechat_space._on_c("c:/users/x"))
        self.assertFalse(wechat_space._on_c("E:\\x"))
        self.assertFalse(wechat_space._on_c("/e/x"))


if __name__ == "__main__":
    unittest.main()
