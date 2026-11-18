"""公众号名 ↔ gh 编号的匹配：OCR 错字也要能对上。

公众号库里有一批名字是从截图或表格里认出来的，``Al-Protein Design`` 里的
"l" 其实是大写 I。这种号在微信里明明关注着，却因为名字差一个字符映射不到
gh 编号，永远进不了追踪名单——表现就是「明明关注了，一条推送都收不到」。

这组用例钉住：折叠规则本身、折叠只在精确匹配失败后才用、以及折叠撞车时
必须放弃匹配（宁可报对不上，也不能把 A 的文章算到 B 头上）。
"""
import unittest
from unittest import mock

from app.services import wechat_self_archive as archive
from app.utils import name_match_key


class NameMatchKeyTest(unittest.TestCase):
    def test_folds_ocr_confusables(self):
        self.assertEqual(name_match_key("Al-Protein Design"), name_match_key("AI-Protein Design"))
        self.assertEqual(name_match_key("ScienceAl"), name_match_key("ScienceAI"))
        self.assertEqual(name_match_key("A14Protein"), name_match_key("AI4Protein"))
        self.assertEqual(name_match_key("Al4Bio小猪莹"), name_match_key("AI4Bio小猪莹"))

    def test_drops_spaces_and_separators(self):
        self.assertEqual(name_match_key("ChinCheml ett"), name_match_key("ChinChem Lett"))
        self.assertEqual(name_match_key("Al-powered Molecular Modell."),
                         name_match_key("AI-poweredMolecularModell"))

    def test_keeps_different_names_different(self):
        self.assertNotEqual(name_match_key("BioArt"), name_match_key("BioArtPlant"))
        self.assertNotEqual(name_match_key("酶言酶语"), name_match_key("酶语酶言"))


class _FakeDb:
    def __init__(self, accounts):
        self._accounts = accounts

    def query(self, sql, params=()):
        return self._accounts


class PublicAccountFilterTest(unittest.TestCase):
    def _run(self, accounts, directory, use_catalog=True):
        fake = _FakeDb(accounts)
        with mock.patch("app.db.Database", return_value=fake):
            return archive._public_account_filter({"use_catalog": use_catalog}, directory)

    def test_exact_name_still_wins(self):
        directory = {
            "gh_real": {"name": "AI-Protein Design", "aliases": {"ai-protein design"}},
            "gh_other": {"name": "别的号", "aliases": {"别的号"}},
        }
        allowed, matched, unmatched = self._run(
            [{"name": "AI-Protein Design", "account_id": None}], directory)
        self.assertEqual(allowed, {"gh_real"})
        self.assertEqual(unmatched, 0)

    def test_ocr_misspelling_still_matches(self):
        """库里写成 Al（小写 L）也要认到 AI-Protein Design。"""
        directory = {
            "gh_54f27925507e": {"name": "AI-Protein Design",
                                "aliases": {"ai-protein design", "aiproteindesign"}},
        }
        allowed, matched, unmatched = self._run(
            [{"name": "Al-Protein Design", "account_id": None}], directory)
        self.assertEqual(allowed, {"gh_54f27925507e"},
                         "OCR 错字的号必须能进名单，否则它的推文永远收不到")
        self.assertEqual(unmatched, 0)

    def test_ambiguous_fold_is_rejected(self):
        """折叠后撞车时不能瞎认——那会把 A 的推文算到 B 头上。

        目录里两个号折叠后都是 ``ceii``，库里给的名字 ``CelI`` 精确匹配不到任何
        一个，退到折叠层时命中两个：这种情况必须按"对不上"处理。
        """
        directory = {
            "gh_a": {"name": "Cel1", "aliases": {"cel1"}},
            "gh_b": {"name": "Cell", "aliases": {"cell"}},
        }
        allowed, matched, unmatched = self._run(
            [{"name": "CelI", "account_id": None}], directory)
        self.assertEqual(allowed, set(), "撞车时应按对不上处理")
        self.assertEqual(unmatched, 1)

    def test_account_id_short_circuits_name_matching(self):
        directory = {"gh_x": {"name": "随便", "aliases": {"随便"}}}
        allowed, _, _ = self._run(
            [{"name": "完全不同的名字", "account_id": "gh_x"}], directory)
        self.assertEqual(allowed, {"gh_x"}, "有 account_id 就不该再看名字")

    def test_catalog_off_ignores_names(self):
        directory = {"gh_x": {"name": "AI-Protein Design", "aliases": {"ai-protein design"}}}
        allowed, _, _ = self._run([{"name": "AI-Protein Design", "account_id": None}],
                                  directory, use_catalog=False)
        self.assertEqual(allowed, set())


if __name__ == "__main__":
    unittest.main()
