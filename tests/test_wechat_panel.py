"""公众号面板这一层的回归测试：卡片解析、状态判定、补录、排除与恢复。

面板读的是本机微信的 PublicMsg.db，所以卡片解析必须同时吃掉两种真实形态：
新版带 `mmreader/category/item`（有封面图和准确的 pub_time），老版只有顶层
`appmsg`（只有标题和链接）。补录和排除会写库，测试全部落在临时数据库里，
不碰正式资料库。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.services import filters, wechat3_public, wechat4_live, wechat_panel, wechat_public, wechat_self_archive

# 真实卡片的长相（截取关键字段）：封面和发布时间藏在 mmreader/category/item 里。
CARD_WITH_COVER = """<msg><appmsg appid="" sdkver="0">
<title><![CDATA[胶原蛋白合成过程]]></title>
<des><![CDATA[胶原蛋白合成的每一步均受酶和辅因子调控]]></des>
<type>5</type>
<url><![CDATA[http://mp.weixin.qq.com/s?__biz=MzY0MDA3MjY3Ng==&mid=2247485268&idx=1&sn=95f00331]]></url>
<mmreader><category type="20" count="1"><name><![CDATA[蛋白大全]]></name>
<item><title><![CDATA[胶原蛋白合成过程]]></title>
<url><![CDATA[http://mp.weixin.qq.com/s?__biz=MzY0MDA3MjY3Ng==&mid=2247485268&idx=1&sn=95f00331]]></url>
<pub_time>1794832020</pub_time>
<summary><![CDATA[胶原蛋白是动物体内含量最丰富的结构蛋白]]></summary>
<cover><![CDATA[https://mmbiz.qpic.cn/sz_mmbiz_jpg/abc/640?wxtype=jpeg]]></cover>
</item></category></mmreader>
</appmsg></msg>"""

# 早期版本的消息没有 mmreader：只取得到标题、简介和链接，没有封面。
CARD_PLAIN = """<msg><appmsg appid="" sdkver="0">
<title><![CDATA[一篇老文章]]></title>
<des><![CDATA[简介在这里]]></des>
<type>5</type>
<url><![CDATA[http://mp.weixin.qq.com/s?__biz=AAA&mid=1&idx=1&sn=x]]></url>
</appmsg></msg>"""


class CardParsingTest(unittest.TestCase):
    def test_prefers_item_node_for_cover_and_time(self):
        card = wechat_public.parse_card(CARD_WITH_COVER)
        self.assertEqual(card["title"], "胶原蛋白合成过程")
        self.assertTrue(card["cover"].startswith("https://mmbiz.qpic.cn/"))
        self.assertEqual(card["published_epoch"], 1794832020)
        self.assertEqual(card["source_name"], "蛋白大全")
        self.assertIn("胶原蛋白", card["summary"])

    def test_falls_back_to_plain_appmsg(self):
        card = wechat_public.parse_card(CARD_PLAIN)
        self.assertEqual(card["title"], "一篇老文章")
        self.assertEqual(card["summary"], "简介在这里")
        self.assertEqual(card["cover"], "")
        self.assertEqual(card["published_epoch"], 0)

    def test_never_raises_on_junk(self):
        for junk in ("", "<msg>", "not xml at all", "<msg><appmsg></msg>"):
            self.assertEqual(wechat_public.parse_card(junk), {})


# 一次群发多篇：三个 <appmsg> 并排，前两个是同一篇的两份（参数完整度不同）。
CARD_MULTI = """<msg>
<appmsg><title><![CDATA[上面那篇]]></title><des><![CDATA[摘要一]]></des>
<url><![CDATA[https://mp.weixin.qq.com/s?__biz=AAA&mid=100&idx=1&sn=a1]]></url></appmsg>
<appmsg><title><![CDATA[上面那篇]]></title><des><![CDATA[摘要一]]></des>
<url><![CDATA[https://mp.weixin.qq.com/s?__biz=AAA&mid=100&idx=1&sn=a1&chksm=abc]]></url></appmsg>
<appmsg><title><![CDATA[下面那篇]]></title><des><![CDATA[摘要二]]></des>
<url><![CDATA[https://mp.weixin.qq.com/s?__biz=AAA&mid=100&idx=2&sn=b2&chksm=def]]></url></appmsg>
</msg>"""

# 另一种存法：一个 appmsg 里塞 mmreader/category/item 列表。
CARD_MMREADER_MULTI = """<msg><appmsg><title><![CDATA[外层标题]]></title>
<mmreader><category count="2"><name><![CDATA[某号]]></name>
<item><title><![CDATA[第一篇]]></title>
<url><![CDATA[https://mp.weixin.qq.com/s?__biz=AAA&mid=200&idx=1&sn=c1]]></url>
<pub_time>1794832020</pub_time><summary><![CDATA[摘要甲]]></summary></item>
<item><title><![CDATA[第二篇]]></title>
<url><![CDATA[https://mp.weixin.qq.com/s?__biz=AAA&mid=200&idx=2&sn=c2]]></url>
<pub_time>1794832100</pub_time><summary><![CDATA[摘要乙]]></summary></item>
</category></mmreader></appmsg></msg>"""


class MultiArticleCardTest(unittest.TestCase):
    """一条消息挂多篇时，面板上每一篇都要出现，否则补录都没入口。"""

    def test_expands_sibling_appmsg(self):
        cards = wechat_public.parse_cards(CARD_MULTI)
        self.assertEqual([c["title"] for c in cards], ["上面那篇", "下面那篇"])
        self.assertEqual([c["idx"] for c in cards], [1, 2])

    def test_keeps_richer_url_for_same_article(self):
        cards = wechat_public.parse_cards(CARD_MULTI)
        self.assertIn("chksm=", cards[0]["url"])

    def test_expands_mmreader_items(self):
        cards = wechat_public.parse_cards(CARD_MMREADER_MULTI)
        self.assertEqual([c["title"] for c in cards], ["第一篇", "第二篇"])
        self.assertEqual(cards[1]["published_epoch"], 1794832100)
        self.assertEqual(cards[1]["source_name"], "某号")

    def test_parse_card_still_returns_first(self):
        self.assertEqual(wechat_public.parse_card(CARD_MULTI)["title"], "上面那篇")



class GhIdResolveTest(unittest.TestCase):
    """号名对编号：同名精确匹配优先，放宽匹配必须唯一。"""

    def setUp(self):
        self._saved = wechat_public.account_directory
        wechat_public.account_directory = lambda: {
            "gh_nature": {"name": "Nature", "aliases": {"nature"}},
            "gh_nature_port": {"name": "Nature Portfolio", "aliases": {"natureportfolio"}},
            "gh_bio": {"name": "生物世界", "aliases": {"生物世界", "sworld"}},
        }

    def tearDown(self):
        wechat_public.account_directory = self._saved

    def test_known_id_wins(self):
        self.assertEqual(wechat_public.gh_id_for("随便什么名字", "gh_known"), "gh_known")

    def test_exact_alias(self):
        self.assertEqual(wechat_public.gh_id_for("生物世界"), "gh_bio")

    def test_ambiguous_fuzzy_returns_empty(self):
        # 这个名字同时包含「Nature」和「Nature Portfolio」，放宽匹配会命中两个号，
        # 宁可说对不上，也不能打开 A 看到 B 的推文。
        self.assertEqual(wechat_public.gh_id_for("Nature Portfolio 中文版"), "")

    def test_fuzzy_when_unique(self):
        self.assertEqual(wechat_public.gh_id_for("Nature 系列"), "gh_nature")

    def test_unknown(self):
        self.assertEqual(wechat_public.gh_id_for("完全不存在的公众号"), "")


class PanelActionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.execute("""INSERT INTO wechat_accounts
            (name,account_id,category_code,fit,reason,description,enabled,created_at,updated_at)
            VALUES('蛋白大全','gh_abc','BIOMED','CORE','','',1,'t','t')""")
        self.account = self.db.one("SELECT * FROM wechat_accounts WHERE name='蛋白大全'")

    def article(self, mid, title, url=None):
        return {
            "url": url or f"http://mp.weixin.qq.com/s?__biz=MzY0MDA3MjY3Ng==&mid={mid}&idx=1&sn={mid}",
            "title": title, "summary": "摘要", "cover": "https://mmbiz.qpic.cn/x.jpg",
            "published_at": "2026-11-16T01:00:00+00:00", "message_id": mid,
        }

    def test_state_matrix(self):
        library = {"u1": {"id": "i1", "status": "ready", "excluded_at": None},
                   "u2": {"id": "i2", "status": "discovered", "excluded_at": None}}
        marks = {"u3": {"state": "excluded", "reason": "广告"}}
        self.assertEqual(wechat_panel.state_of("u1", library, marks)[0], "archived")
        self.assertEqual(wechat_panel.state_of("u2", library, marks)[0], "index_only")
        self.assertEqual(wechat_panel.state_of("u3", library, marks), ("excluded", {"reason": "广告"}))
        self.assertEqual(wechat_panel.state_of("u4", library, marks)[0], "new")

    def test_import_creates_item_and_queues_archive(self):
        result = wechat_panel.import_articles(self.db, self.account, "gh_abc", [self.article(1001, "胶原蛋白合成")])
        self.assertEqual(result["created"], 1)
        item = self.db.one("SELECT * FROM items WHERE title='胶原蛋白合成'")
        self.assertIsNotNone(item)
        self.assertEqual(item["author"], "蛋白大全")
        job = self.db.one("SELECT * FROM jobs WHERE kind='archive_item'")
        self.assertEqual(job["payload_json"].count(item["id"]), 1)

    def test_import_is_idempotent(self):
        wechat_panel.import_articles(self.db, self.account, "gh_abc", [self.article(1002, "同一篇")])
        again = wechat_panel.import_articles(self.db, self.account, "gh_abc", [self.article(1002, "同一篇")])
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["existing"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM items")["c"], 1)

    def test_exclude_one_only_touches_that_article(self):
        article = self.article(1003, "某条广告")
        wechat_panel.import_articles(self.db, self.account, "gh_abc", [article])
        result = wechat_panel.exclude_articles(self.db, self.account, "gh_abc", [article],
                                               mode="one", reason="广告")
        self.assertEqual(result["excluded"], 1)
        self.assertIsNone(result["rule_id"])
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM filter_rules WHERE enabled=1")["c"], 0)
        mark = self.db.one("SELECT * FROM wechat_article_marks")
        self.assertEqual(mark["state"], "excluded")
        self.assertEqual(mark["reason"], "广告")
        item = self.db.one("SELECT excluded_at,exclusion_reason FROM items")
        self.assertIsNotNone(item["excluded_at"])
        self.assertTrue(item["exclusion_reason"].startswith("公众号面板排除"))

    def test_exclude_with_rule_blocks_future_matches(self):
        article = self.article(1004, "招聘：实验室招博士后")
        result = wechat_panel.exclude_articles(self.db, self.account, "gh_abc", [article],
                                               mode="rule", reason="招聘 / 招生",
                                               keywords=["招聘", "博士后"])
        rule_id = result["rule_id"]
        self.assertTrue(rule_id)
        rule = self.db.one("SELECT * FROM filter_rules WHERE id=?", (rule_id,))
        self.assertEqual(rule["scope"], "account")
        self.assertEqual(rule["account"], "蛋白大全")
        self.assertIn("招聘", rule["patterns"])
        # 以后同类标题在入库时就该被挡住，这里用过滤器的判断函数验证。
        rules = filters.load_rules(self.db)
        hit = filters.match(rules, "蛋白大全", "博士后招聘启事", "")
        self.assertIsNotNone(hit)
        self.assertIn("招聘", hit["hits"])
        # 别的号发同样的标题不受影响。
        self.assertIsNone(filters.match(rules, "另一个号", "博士后招聘启事", ""))

    def test_restore_clears_mark_and_item(self):
        article = self.article(1005, "被误排除了")
        wechat_panel.import_articles(self.db, self.account, "gh_abc", [article])
        wechat_panel.exclude_articles(self.db, self.account, "gh_abc", [article], mode="one", reason="测试")
        result = wechat_panel.restore_articles(self.db, [article])
        self.assertEqual(result["restored"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM wechat_article_marks")["c"], 0)
        self.assertIsNone(self.db.one("SELECT excluded_at FROM items")["excluded_at"])

    def test_marks_and_library_index_are_scoped_to_given_urls(self):
        mine = self.article(1006, "我的")
        other = self.article(1007, "别人的")
        wechat_panel.import_articles(self.db, self.account, "gh_abc", [mine, other])
        urls = [wechat_panel._safe_canonical(mine["url"])]
        self.assertEqual(len(wechat_panel.library_index(self.db, urls)), 1)
        self.assertEqual(wechat_panel.library_index(self.db, []), {})

    def test_disable_account(self):
        wechat_panel.disable_account(self.db, int(self.account["id"]), "广告太多")
        row = self.db.one("SELECT enabled FROM wechat_accounts WHERE id=?", (self.account["id"],))
        self.assertEqual(row["enabled"], 0)

    def test_library_articles_lists_archived_wechat_items(self):
        """面板要能把资料库已收录的推文补进列表。

        4.x 不落盘正文、3.x 的 PublicMsg.db 又冻结在升级那天，只问微信的话
        列表会停在 16 号，而资料库里 11-19 的条目明明存在。
        """
        wechat_panel.import_articles(self.db, self.account, "gh_abc", [self.article(1101, "已收录的一篇")])
        rows = wechat_panel.library_articles(self.db, "gh_abc", "蛋白大全", days=3650)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "已收录的一篇")
        self.assertEqual(rows[0]["idx"], 1)
        self.assertGreater(rows[0]["create_time"], 0)
        # 换一个号就查不到别人的文章
        self.assertEqual(wechat_panel.library_articles(self.db, "gh_other", "别的号", days=3650), [])

    def test_library_articles_keeps_idx_of_second_post(self):
        second = self.article(1102, "同批第二篇",
                              url="http://mp.weixin.qq.com/s?__biz=B&mid=1102&idx=2&sn=x")
        wechat_panel.import_articles(self.db, self.account, "gh_abc", [second])
        rows = wechat_panel.library_articles(self.db, "gh_abc", "", days=3650)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["idx"], 2)


class PanelArticlesApiTest(unittest.TestCase):
    """面板接口的合并层：本机微信没有的内容，也要从资料库里补回来。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.execute("""INSERT INTO wechat_accounts
            (name,account_id,category_code,fit,reason,description,enabled,created_at,updated_at)
            VALUES('蛋白大全','gh_abc','BIOMED','CORE','','',1,'t','t')""")
        self.account = self.db.one("SELECT * FROM wechat_accounts WHERE name='蛋白大全'")

    def _call(self, **kwargs):
        from app import api
        hint = {"at": 0, "at_display": "", "title": "", "unread": 0, "last_type": 0}
        with patch.object(api, "db", self.db), \
                patch.object(wechat_self_archive, "local_channel", return_value="4.x"), \
                patch.object(wechat4_live, "recent_for", return_value=[]), \
                patch.object(wechat4_live, "session_hint", return_value=hint), \
                patch.object(wechat3_public, "recent_for", return_value=[]):
            return api.wechat_account_articles(int(self.account["id"]), **kwargs)

    def test_library_items_fill_the_gap_left_by_wechat(self):
        article = {"url": "http://mp.weixin.qq.com/s?__biz=B&mid=9001&idx=1&sn=x",
                   "title": "16 号之后的推文", "summary": "摘要",
                   "published_at": "2026-11-19T08:30:00+00:00", "message_id": 9001}
        wechat_panel.import_articles(self.db, self.account, "gh_abc", [article])
        self.db.execute("UPDATE items SET status='ready' WHERE title=?", ("16 号之后的推文",))
        payload = self._call(limit=10, offset=0, days=60)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["title"], "16 号之后的推文")
        self.assertEqual(payload["items"][0]["state"], "archived")

    def test_pagination_still_honours_limit(self):
        for mid, day in ((9101, 11), (9102, 12), (9103, 13)):
            wechat_panel.import_articles(self.db, self.account, "gh_abc", [{
                "url": f"http://mp.weixin.qq.com/s?__biz=B&mid={mid}&idx=1&sn=x",
                "title": f"第 {mid} 篇", "summary": "",
                "published_at": f"2026-11-{day:02d}T08:30:00+00:00", "message_id": mid}])
        first = self._call(limit=2, offset=0, days=60)
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["has_more"])
        second = self._call(limit=2, offset=2, days=60)
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse(second["has_more"])


if __name__ == "__main__":
    unittest.main()
