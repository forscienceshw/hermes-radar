"""资料库这一层的回归测试：引流识别、自定义过滤规则、图片消息降级。

这三块都在「入库」这条线上，判错了直接影响用户看到的资料库，
所以每条判据都要有正例和反例，不能只测「能挡住」。
"""
import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.services import filters
from app.services.archive import detect_verify_page
from app.services.relevance import detect_marketing

ROOT = Path(__file__).resolve().parents[1]


def _temp_root() -> Path:
    root = ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


# 一段真实的软文正文：开头就让你扫码下载，通篇没有 DOI、参考文献和图表编号。
SOFT_AD = (
    "随着电子系统向高集成、低功耗方向演进，电源管理已成为关键技术。"
    "本文围绕 ADI 电源技术生态，介绍 LDO 与降压稳压器的产品特征。"
    "👇扫码下载PDF资料 一、LDO 的低噪声设计要点……" + "参数与选型说明。" * 40 +
    "📥 立即下载 说了这么多，不如直接看原文。建议先下载存着。"
)

# 一篇正常的研究解读：正文讲机制，末尾才是公众号例行的二维码。
REAL_ARTICLE = (
    "研究团队用单分子光镊测量了 ClpX 对底物蛋白的易位步长，发现四步一个循环，"
    "每一步约 5 纳米。图1 展示了不同 ATP 浓度下的步进轨迹，图2 是突变体的对照。"
    "参考文献：Olivares et al., Nature 2025. doi:10.1038/s41586-025-01234-5. "
    + "作者进一步讨论了力学-化学耦合的模型。" * 20 +
    "▲长按识别二维码关注我们"
)


class MarketingDetectTests(unittest.TestCase):
    def test_blocks_soft_ad_with_top_cta(self):
        verdict = detect_marketing(SOFT_AD)
        self.assertTrue(verdict["blocked"])
        self.assertGreaterEqual(verdict["score"], 2)

    def test_footer_cta_does_not_block(self):
        verdict = detect_marketing(REAL_ARTICLE)
        self.assertFalse(verdict["blocked"], verdict["reason"])

    def test_academic_evidence_wins_even_with_top_cta(self):
        body = "扫码下载资料。本文 DOI 为 10.1101/2025.01.01.123456，图3 给出了对照结果。" + "分析。" * 80
        self.assertFalse(detect_marketing(body)["blocked"])

    def test_short_text_is_never_blocked(self):
        self.assertFalse(detect_marketing("扫码下载")["blocked"])
        self.assertFalse(detect_marketing("")["blocked"])


class VerifyPageTests(unittest.TestCase):
    PAGE = ('<script>window.cgiData={cap_sid:"1"};seajs.use("secitptpage/template/verify.js")</script>'
            '<script src="https://captcha.gtimg.com/TCaptcha.js"></script>'
            'target_url:"https://mp.weixin.qq.com/s?t=pages/image_detail"')

    def test_detects_image_post_verify_page(self):
        reason = detect_verify_page(self.PAGE)
        self.assertIn("图片消息", reason)

    def test_plain_page_is_not_verify(self):
        self.assertEqual(detect_verify_page("<html>普通文章</html>"), "")


class FilterRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.execute("""INSERT INTO filter_rules(scope,account,field,patterns,match_mode,note,enabled,hits,
            created_at,updated_at) VALUES('account','某材料号','title','招聘|会议通知','any','',1,0,'t','t')""")
        self.db.execute("""INSERT INTO filter_rules(scope,account,field,patterns,match_mode,note,enabled,hits,
            created_at,updated_at) VALUES('all','','title','AI','any','',1,0,'t','t')""")

    def tearDown(self):
        self.temp.cleanup()

    def _add(self, title, author, description="", url_suffix="1"):
        item_id, _ = self.db.discover_item({
            "url": f"https://mp.weixin.qq.com/s?__biz=a&mid=1&idx={url_suffix}",
            "kind": "article", "title": title, "author": author,
            "metadata": {"description": description},
        })
        return item_id

    def test_account_scoped_rule_matches_title(self):
        rules = filters.load_rules(self.db)
        hit = filters.match(rules, "某材料号", "课题组招聘科研助理", "")
        self.assertIsNotNone(hit)
        self.assertIn("招聘", hit["hits"])

    def test_rule_does_not_leak_to_other_accounts(self):
        rules = filters.load_rules(self.db)
        self.assertIsNone(filters.match(rules, "别的号", "课题组招聘科研助理", ""))

    def test_header_field_includes_description(self):
        self.db.execute("""INSERT INTO filter_rules(scope,account,field,patterns,match_mode,note,enabled,hits,
            created_at,updated_at) VALUES('all','','header','夏令营','any','',1,0,'t','t')""")
        rules = filters.load_rules(self.db)
        self.assertIsNotNone(filters.match(rules, "任何号", "标题无关", "报名夏令营见文末"))

    def test_ascii_pattern_needs_word_boundary(self):
        rules = filters.load_rules(self.db)
        # 规则里的 "AI" 不该命中 "said"
        self.assertIsNone(filters.match(rules, "某号", "said the author", ""))
        self.assertIsNotNone(filters.match(rules, "某号", "AI 辅助的药物筛选", ""))

    def test_apply_and_restore_round_trip(self):
        blocked = self._add("课题组招聘科研助理", "某材料号", "", "2")
        kept = self._add("钙钛矿薄膜的缺陷钝化", "某材料号", "", "3")
        changed = filters.apply_to_existing(self.db)
        self.assertEqual(changed, 1)
        self.assertIsNotNone(self.db.one("SELECT excluded_at FROM items WHERE id=?", (blocked,))["excluded_at"])
        self.assertIsNone(self.db.one("SELECT excluded_at FROM items WHERE id=?", (kept,))["excluded_at"])
        reason = self.db.one("SELECT exclusion_reason FROM items WHERE id=?", (blocked,))["exclusion_reason"]
        self.assertTrue(reason.startswith(filters.REASON_PREFIX))
        rule_id = int(reason.split("#", 1)[1].split("：", 1)[0])
        self.assertEqual(filters.restore_by_rule(self.db, rule_id), 1)
        self.assertIsNone(self.db.one("SELECT excluded_at FROM items WHERE id=?", (blocked,))["excluded_at"])

    def test_split_patterns_accepts_mixed_separators(self):
        self.assertEqual(filters.split_patterns("招聘，招生\n会议通知|展会；周报"),
                         ["招聘", "招生", "会议通知", "展会", "周报"])

    def test_describe_is_human_readable(self):
        rules = filters.load_rules(self.db)
        scoped = next(rule for rule in rules if rule["scope"] == "account")
        text = filters.describe(scoped)
        self.assertIn("某材料号", text)
        self.assertIn("招聘", text)
        every = next(rule for rule in rules if rule["scope"] == "all")
        self.assertIn("所有公众号", filters.describe(every))
        self.assertIn("AI", filters.describe(every))


class LibraryVisibilityTests(unittest.TestCase):
    """低相关是中间档，默认照旧显示；勾了「隐藏低相关」才让位。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")
        self.ids = []
        for index, title in enumerate(("有价值的综述", "没什么用的一篇")):
            item_id, _ = self.db.discover_item({
                "url": f"https://mp.weixin.qq.com/s?__biz=a&mid=1&idx={index}", "kind": "article",
                "title": title, "author": "某号", "metadata": {}})
            self.ids.append(item_id)
        self.db.execute("UPDATE items SET status='ready' WHERE id=?", (self.ids[0],))
        self.db.execute("UPDATE items SET status='filtered' WHERE id=?", (self.ids[1],))

    def tearDown(self):
        self.temp.cleanup()

    def test_shown_by_default(self):
        rows = self.db.search(limit=10)
        self.assertEqual(len(rows), 2, "低相关不该默认消失，判断不一定准，错过了补不回来")

    def test_hidden_when_toggled(self):
        rows = self.db.search(limit=10, hide_status="filtered")
        self.assertEqual([row["title"] for row in rows], ["有价值的综述"])
        self.assertEqual(self.db.search_count(hide_status="filtered"), 1)

    def test_visible_when_status_is_explicit(self):
        rows = self.db.search(status="filtered", limit=10, hide_status="filtered")
        self.assertEqual([row["title"] for row in rows], ["没什么用的一篇"])


if __name__ == "__main__":
    unittest.main()
