"""4.x 内存通道的回归测试。

这条路的失败模式很特别：解析错了会静默返回空列表，看起来"通道正常但今天
没有推送"，和"微信真的没推"长得一模一样。所以钉四件事：卡片正则、首次
全收、增量去重、名单过滤。

不依赖真实微信进程——``scan()`` 全部用假的 Snapshot 替掉。
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from app.services import wechat4_memory as mem
from app.services import wechat_self_archive as archive


def _card(gh: str, mid: str, ts: int, title: str, url: str = "") -> dict:
    return {"gh": gh, "mid": mid, "ts": ts, "title": title, "key": f"{gh}@{mid}",
            "url": url, "canonical_url": mem.canonical_url(url) if url else ""}


class CardRegexTest(unittest.TestCase):
    def test_parses_box_card(self):
        raw = (b'{"AppMsgId":2247487245,"DateTime":1794968546,"weight":200,'
               b'"uniqueId":"gh_b454d3a6ed68@2247487245","localId":1808050764,'
               b'"Title":"\xe8\xa5\xbf\xe5\x8c\x97\xe5\x86\x9c\xe5\xa4\xa7 Cell Research"}')
        m = mem.CARD_RE.search(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(3), b"gh_b454d3a6ed68")
        self.assertEqual(m.group(4), b"2247487245")
        self.assertEqual(m.group(2), b"1794968546")
        self.assertEqual(mem._decode_json_string(m.group(6)), "西北农大 Cell Research")

    def test_decodes_escaped_quotes(self):
        raw = b'Anal. Chem. | \\"\xe4\xb8\x80\xe7\xa7\x8d\xe6\x8e\xa2\xe9\x92\x88\\"'
        text = mem._decode_json_string(raw)
        self.assertIn("Anal. Chem.", text)
        self.assertIn("一种探针", text)
        self.assertIn('"', text)

    def test_ignores_non_gh_cards(self):
        raw = (b'{"AppMsgId":"ad_card1","DateTime":1794968546,"weight":0,'
               b'"uniqueId":"ad_card1@ad_card1","localId":465352475,"Title":""}')
        m = mem.CARD_RE.search(raw)
        self.assertIsNotNone(m)
        self.assertFalse(m.group(3).decode().startswith("gh_"))


class _FakeScan:
    def __init__(self, cards):
        self.cards = cards
        self.urls = {c["mid"]: c["url"] for c in cards if c["url"]}
        self.accounts = {c["gh"]: "号" + c["gh"][-4:] for c in cards}
        self.at = time.time()

    @property
    def ok(self):
        return True

    reason = ""
    elapsed = 0.1
    scanned_mb = 10.0


class MemoryCandidateTest(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.source = {"id": "src-1"}
        self.config = {"memory_channel": True, "initial_backfill_days": 7, "memory_max_new": 300}
        self.names = {"gh_ok": "科研号"}
        self.cards = [
            _card("gh_ok", "1001", self.now - 60, "新论文 A", "https://mp.weixin.qq.com/s?__biz=AAA&mid=1001&idx=1&sn=x"),
            _card("gh_ok", "1000", self.now - 120, "旧论文 B", "https://mp.weixin.qq.com/s?__biz=AAA&mid=1000&idx=1&sn=y"),
            _card("gh_out", "2001", self.now - 90, "广告", "https://mp.weixin.qq.com/s?__biz=BBB&mid=2001&idx=1&sn=z"),
        ]

    def _run(self, previous, allowed={"gh_ok"}):
        with mock.patch.object(mem, "scan", return_value=_FakeScan(self.cards)):
            return archive._wechat4_memory_candidates(
                self.source, self.config, previous, self.names, allowed, True, set())

    def test_first_run_takes_whitelist_only(self):
        out, cursor = self._run({})
        self.assertEqual([c["external_key"] for c in out],
                         ["public4mem:gh_ok:1001", "public4mem:gh_ok:1000"])
        self.assertEqual(cursor["kept"], 2)
        self.assertEqual(cursor["scanned"], 3)
        for cand in out:
            self.assertEqual(cand["metadata"]["share_channel"], "wechat_memory_box")
            self.assertEqual(cand["author"], "科研号")
            self.assertTrue(cand["url"].startswith("https://mp.weixin.qq.com/s"))

    def test_second_run_is_incremental(self):
        _, cursor = self._run({})
        out2, _ = self._run({"memory": cursor})
        self.assertEqual(out2, [])

    def test_outsider_not_recorded_so_it_can_be_added_later(self):
        _, cursor = self._run({})
        self.assertNotIn("gh_out@2001", cursor["seen"])
        out3, _ = self._run({"memory": cursor}, allowed={"gh_ok", "gh_out"})
        self.assertEqual([c["external_key"] for c in out3], ["public4mem:gh_out:2001"])

    def test_card_without_url_waits(self):
        self.cards.append(_card("gh_ok", "1002", self.now - 30, "还没拿到链接", ""))
        out, cursor = self._run({})
        self.assertEqual(len(out), 2)
        self.assertEqual(cursor["pending_url"], 1)
        self.assertNotIn("gh_ok@1002", cursor["seen"])

    def test_old_cards_are_dropped(self):
        self.cards.append(_card("gh_ok", "999", self.now - 30 * 86400, "陈年旧文",
                                "https://mp.weixin.qq.com/s?__biz=AAA&mid=999&idx=1&sn=o"))
        out, _ = self._run({})
        self.assertNotIn("public4mem:gh_ok:999", [c["external_key"] for c in out])

    def test_channel_switch_off(self):
        self.config["memory_channel"] = False
        out, cursor = self._run({})
        self.assertEqual(out, [])
        self.assertIn("关闭", cursor["reason"])

    def test_unavailable_weixin_keeps_cursor(self):
        bad = mem.Snapshot(at=time.time(), reason="Weixin.exe 没有运行")
        with mock.patch.object(mem, "scan", return_value=bad):
            out, cursor = archive._wechat4_memory_candidates(
                self.source, self.config, {}, self.names, {"gh_ok"}, True, set())
        self.assertEqual(out, [])
        self.assertFalse(cursor["available"])
        self.assertIn("没有运行", cursor["reason"])


if __name__ == "__main__":
    unittest.main()
