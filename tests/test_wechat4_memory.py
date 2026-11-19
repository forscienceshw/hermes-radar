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

    def test_url_stops_at_control_bytes(self):
        """堆里的 URL 后面常跟二进制字段；带上 \\x02 会让 httpx 直接拒发。"""
        raw = (b"https://mp.weixin.qq.com/s?__biz=MzE5&mid=2247485001&idx=1&sn=abc123"
               b"\x02\x00\x00rest")
        m = mem.URL_RE.search(raw)
        self.assertIsNotNone(m)
        url = m.group(0).decode()
        self.assertNotIn("\x02", url)
        self.assertEqual(mem.MID_IN_URL.search(url).group(1), "2247485001")


class TwoShapeExtractionTest(unittest.TestCase):
    """盒子里有两种 JSON 形态，缺一种就会漏掉一半以上推送。

    A 形态（uniqueId + Title）有 gh 和标题但没有链接；B 形态（detailInfo +
    ContentUrl + showName）有链接但没有 Title。只按 A 的整对象正则去匹配，
    本机实测一次只有 84 张卡片、31 条带链接；两形态按 mid 合并后是 225 / 190。
    """

    def test_unique_id_pairs_gh_with_mid(self):
        raw = (b'{"AppMsgId":2247487245,"DateTime":1794968546,"weight":200,'
               b'"uniqueId":"gh_b454d3a6ed68@2247487245","localId":1,"Title":"x"}')
        m = mem.UNIQ_RE.search(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), b"gh_b454d3a6ed68")
        self.assertEqual(m.group(2), b"2247487245")

    def test_title_window_stops_before_next_element(self):
        raw = (b'"AppMsgId":111111,"DateTime":1794968546,"isPreview":false,'
               b'"detailInfo":{"AudioDuRation":0},"Title":"\u7b2c\u4e00\u6761"}'
               b'{"AppMsgId":222222,"DateTime":1794968550,"Title":"\u7b2c\u4e8c\u6761"}')
        heads = list(mem.HEAD_RE.finditer(raw))
        self.assertEqual(len(heads), 2)
        win0 = raw[heads[0].start():heads[1].start()]
        self.assertEqual(mem._decode_json_string(mem.TITLE_RE.search(win0).group(1)), "第一条")
        win1 = raw[heads[1].start():]
        self.assertEqual(mem._decode_json_string(mem.TITLE_RE.search(win1).group(1)), "第二条")

    def test_assemble_merges_by_mid_and_keeps_partial(self):
        gh_of = {"100": "gh_a", "200": "gh_b", "300": "gh_c"}
        ts_of = {"100": 1794968546, "200": 1794968600, "300": 0}
        title_of = {"100": "有标题也有链接", "200": "只有标题"}
        mid_url = {"100": "https://mp.weixin.qq.com/s?__biz=A&mid=100&idx=1&sn=x",
                   "300": "https://mp.weixin.qq.com/s?__biz=C&mid=300&idx=1&sn=y"}
        cards = mem._assemble_cards(gh_of, ts_of, title_of, mid_url)
        self.assertEqual([c["mid"] for c in cards], ["200", "100", "300"], "必须按时间倒序")
        by_mid = {c["mid"]: c for c in cards}
        self.assertEqual(by_mid["100"]["url"], mid_url["100"])
        self.assertTrue(by_mid["100"]["canonical_url"])
        self.assertEqual(by_mid["200"]["url"], "", "只有标题也要先收下，等下一轮补链接")
        self.assertEqual(by_mid["300"]["title"], "", "只有链接也要收，标题可以后补")

    def test_assemble_drops_cards_with_nothing(self):
        cards = mem._assemble_cards({"400": "gh_d"}, {"400": 1794968546}, {}, {})
        self.assertEqual(cards, [], "既没标题又没链接的空壳不该进队列")

    def test_assemble_keeps_every_article_of_one_broadcast(self):
        """一次群发多篇：mid 相同、idx 不同，每篇都要出卡片。

        2026-11-19 用户报「同时推两篇，只读到上面那篇」。旧写法按 mid 归并，
        idx=2 起全被 setdefault 吞掉；实测进程堆里 101 个 mid 带多个 idx。
        """
        gh_of = {"500": "gh_e"}
        ts_of = {"500": 1795055495}
        title_of = {"500": ["上篇标题", "下篇标题"]}
        mid_url = {"500": {1: "https://mp.weixin.qq.com/s?__biz=E&mid=500&idx=1&sn=a",
                           2: "https://mp.weixin.qq.com/s?__biz=E&mid=500&idx=2&sn=b"}}
        cards = mem._assemble_cards(gh_of, ts_of, title_of, mid_url)
        self.assertEqual([c["idx"] for c in cards], [1, 2], "同一次群发按 idx 升序")
        self.assertEqual([c["title"] for c in cards], ["上篇标题", "下篇标题"])
        self.assertEqual(cards[0]["key"], "gh_e@500", "idx=1 保持旧 key，历史 seen 仍能去重")
        self.assertEqual(cards[1]["key"], "gh_e@500#2")
        self.assertTrue(all(c["url"] for c in cards))

    def test_assemble_leaves_title_empty_when_count_is_short(self):
        """标题只读到一条却有两篇时，第二篇宁可留空，也不许张冠李戴。"""
        cards = mem._assemble_cards(
            {"600": "gh_f"}, {"600": 1795055495}, {"600": ["只有第一条标题"]},
            {"600": {1: "https://mp.weixin.qq.com/s?mid=600&idx=1",
                     2: "https://mp.weixin.qq.com/s?mid=600&idx=2"}})
        self.assertEqual(cards[0]["title"], "只有第一条标题")
        self.assertEqual(cards[1]["title"], "", "配不上就留空，入库时用 URL 兜底")

    def test_url_score_prefers_clean_https(self):
        clean = "https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=abc"
        dirty = "http://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=abc\x02\ufffd"
        self.assertGreater(mem._url_score(clean), mem._url_score(dirty))


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
