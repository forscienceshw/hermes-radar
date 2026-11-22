"""详情页「先给缓存、后台补读」的回归测试。

2026-11-22 的事故：首次启动后第一次点某个公众号，详情页要扫一遍 Weixin.exe
的内存（1.1 GB），请求几秒到十几秒才回来；那段时间弹层里只有「正在读取…」，
用户以为卡死，关掉再点（此时缓存已被第一次写好）就正常了。

这里锁住三件事：
* 没有缓存时 peek 必须立刻返回 warming，而不是等扫完；
* 同一个号同时只允许一个后台刷新线程；
* 「空列表」的原因要能分清：微信没运行 / 没关注 / 真没推送 / 读取失败。
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.services import shell, wechat_detail, wechat_panel
from app.utils import now_iso


def _item(title: str, ts: int, *, slug: str) -> dict:
    return {"canonical_url": f"https://mp.weixin.qq.com/s?__biz=x&mid=1&idx=1&sn={slug}",
            "url": f"https://mp.weixin.qq.com/s?__biz=x&mid=1&idx=1&sn={slug}",
            "title": title, "digest": "摘要", "create_time": ts, "local_id": 1, "idx": 1}


class PeekTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.db")
        wechat_detail.forget(11)          # 模块级编号缓存是跨用例的，先清掉
        self.account = {"id": 11, "name": "测试号", "account_id": "gh_test", "category_code": "OTHER",
                        "fit": "OPTIONAL", "enabled": 1, "description": ""}
        self.db.execute("""INSERT INTO wechat_accounts
            (id,name,account_id,category_code,fit,reason,description,enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (11, "测试号", "gh_test", "OTHER", "OPTIONAL", "", "", 1, now_iso(), now_iso()))
        self.sources = {"channel": "4.x", "wechat_running": True, "in_contacts": True,
                        "db_synced_at": "", "newest_item_at": 0, "days_since": None}
        self.gh = patch.object(wechat_panel, "resolve_gh", return_value=("gh_test", "4.x"))
        self.src = patch.object(wechat_detail, "sources", return_value=dict(self.sources))
        self.fp = patch.object(wechat_panel, "_db_fingerprint", return_value="")
        self.fresh = patch("app.services.wechat4_live.last_refresh_age", return_value=0.0)
        for ctx in (self.gh, self.src, self.fp, self.fresh):
            ctx.start()
            self.addCleanup(ctx.stop)

    def _write_cache(self, items, *, signal=0, age_seconds=0):
        meta = {"channel": "4.x", "note": "", "signal": signal}
        wechat_panel._cache_write(self.db, 11, items, meta, signal=signal)
        if age_seconds:
            stamp = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() - age_seconds, timezone.utc)
            self.db.execute("UPDATE wechat_article_cache SET synced_at=? WHERE account_pk=11",
                            (stamp.isoformat(timespec="seconds"),))

    def test_cold_open_returns_immediately_and_starts_refresh(self):
        with patch.object(wechat_detail, "schedule", return_value=True) as schedule:
            payload = wechat_detail.peek(self.db, self.account, limit=10, days=30)
        self.assertEqual(payload["state"], "warming")
        self.assertTrue(payload["refreshing"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(schedule.call_count, 1)
        self.assertIn("正在读取本机微信", payload["note"])

    def test_fresh_cache_is_returned_without_refresh(self):
        now = int(datetime.now(timezone.utc).timestamp())
        self._write_cache([_item("最近的推文", now, slug="a")], signal=now)
        with patch.object(wechat_detail, "schedule") as schedule:
            payload = wechat_detail.peek(self.db, self.account, limit=10, days=30)
        self.assertEqual(payload["state"], "ok")
        self.assertFalse(payload["refreshing"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["title"], "最近的推文")
        schedule.assert_not_called()

    def test_old_cache_is_shown_while_refreshing(self):
        old = int(datetime.now(timezone.utc).timestamp()) - 40 * 86400
        self._write_cache([_item("很久以前", old, slug="b")], signal=old, age_seconds=3600)
        with patch.object(wechat_detail, "schedule", return_value=True):
            payload = wechat_detail.peek(self.db, self.account, limit=10, days=3650)
        # 有旧内容就先给旧内容，但同时说明正在重读，而不是空着等
        self.assertEqual(payload["total"], 1)
        self.assertTrue(payload["refreshing"])
        self.assertIn(payload["state"], ("ok", "stale"))

    def test_new_push_signal_invalidates_cache(self):
        now = int(datetime.now(timezone.utc).timestamp())
        self._write_cache([_item("旧推文", now - 3600, slug="c")], signal=now - 3600)
        with patch.object(wechat_detail, "schedule", return_value=True) as schedule, \
             patch.object(wechat_panel, "newest_signal", return_value=now):
            wechat_detail.peek(self.db, self.account, limit=10, days=30)
        schedule.assert_called_once()

    def test_no_account_id_is_an_error_not_empty(self):
        broken = {**self.account, "account_id": ""}
        with patch.object(wechat_panel, "resolve_gh", return_value=("", "4.x")):
            payload = wechat_detail.peek(self.db, broken, limit=10, days=30)
        self.assertEqual(payload["state"], "error")
        self.assertIn("编号", payload["error"])

    def test_single_flight(self):
        started = []

        def slow(_db, _account):
            started.append(1)
            import time
            time.sleep(0.25)
            return {"items": [], "meta": {}, "gh_id": "gh_test", "channel": "4.x"}

        with patch.object(wechat_panel, "refresh_cached", side_effect=slow):
            first = wechat_detail.schedule(self.db, self.account)
            second = wechat_detail.schedule(self.db, self.account)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(started), 1)
        for _ in range(30):
            if not wechat_detail.is_refreshing(11):
                break
            import time
            time.sleep(0.05)
        self.assertFalse(wechat_detail.is_refreshing(11))


class ExplainTest(unittest.TestCase):
    def test_wechat_not_running(self):
        text = wechat_detail.explain("empty", {"wechat_running": False}, "", 0)
        self.assertIn("微信现在没有运行", text)

    def test_not_followed(self):
        text = wechat_detail.explain("empty", {"wechat_running": True, "in_contacts": False}, "", 0)
        self.assertIn("关注列表里没有这个号", text)

    def test_really_no_push(self):
        text = wechat_detail.explain("empty", {"wechat_running": True, "in_contacts": True}, "", 0)
        self.assertIn("还没有这个号的推送记录", text)

    def test_specific_note_wins(self):
        self.assertEqual(wechat_detail.explain("empty", {}, "微信只留了会话摘要", 0), "微信只留了会话摘要")

    def test_warming(self):
        self.assertIn("正在读取本机微信", wechat_detail.explain("warming", {}, "", 0))


class ShellPrefsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.patches = [patch.object(shell, "SHELL_FILE", base / "shell.json"),
                        patch.object(shell, "CMD_FILE", base / "shell_cmd.json"),
                        patch.object(shell, "ALIVE_FILE", base / "shell_alive.json")]
        for ctx in self.patches:
            ctx.start()
            self.addCleanup(ctx.stop)

    def test_defaults(self):
        data = shell.load()
        self.assertEqual(data["close_action"], "tray")
        self.assertTrue(data["open_on_start"])

    def test_save_and_reject_unknown_value(self):
        shell.save(close_action="quit")
        self.assertEqual(shell.load()["close_action"], "quit")
        shell.save(close_action="whatever")
        self.assertEqual(shell.load()["close_action"], "tray")

    def test_command_is_taken_once(self):
        shell.send("open")
        self.assertEqual(shell.take(), "open")
        self.assertEqual(shell.take(), "")

    def test_stale_command_is_dropped(self):
        shell.send("quit")
        import json
        data = json.loads(shell.CMD_FILE.read_text("utf-8"))
        data["at"] = 0
        shell.CMD_FILE.write_text(json.dumps(data), "utf-8")
        self.assertEqual(shell.take(), "")

    def test_heartbeat(self):
        self.assertFalse(shell.alive())
        shell.heartbeat()
        self.assertTrue(shell.alive())


if __name__ == "__main__":
    unittest.main()
