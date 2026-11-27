"""报错人话化 + 论文匹配这两块的回归测试。

这两处都属于「判错了用户直接看得见」的地方：
- 报错里落进一整页 HTML，资料库列表会变成一堵墙；
- 论文按标题匹配时误配，会把不相干的 PDF 挂到文章下面。
所以每条判据都要有正例和反例。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import Database
from app.services.papers import candidate_titles, doi_from_links, title_similarity
from app.utils import humanize_error_text, job_error_is_retryable, now_iso

ROOT = Path(__file__).resolve().parents[1]


def _temp_root() -> Path:
    root = ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root

WAF_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WAF拦截页面</title></head>
<body><h1>您的请求已被拦截</h1><p>如有疑问请联系管理员</p>""" + "<div>x</div>" * 500 + "</body></html>"

NORMAL_ERROR = "读取正文超时：连接 api.example.com 时超时（第 2 次重试）"


class HumanizeErrorTest(unittest.TestCase):
    def test_plain_error_is_kept(self):
        self.assertEqual(humanize_error_text(NORMAL_ERROR), NORMAL_ERROR)

    def test_html_page_becomes_one_sentence(self):
        text = humanize_error_text(WAF_PAGE)
        self.assertNotIn("<div", text)
        self.assertLess(len(text), 200)
        self.assertIn("WAF拦截页面", text)
        self.assertIn("重试", text)

    def test_promoted_message_is_retryable(self):
        self.assertTrue(job_error_is_retryable(humanize_error_text(WAF_PAGE)))

    def test_plain_timeout_is_retryable(self):
        self.assertTrue(job_error_is_retryable(NORMAL_ERROR))

    def test_missing_page_is_not_retryable(self):
        self.assertFalse(job_error_is_retryable("微信返回 404，原文可能已被删除"))

    def test_empty(self):
        self.assertEqual(humanize_error_text(""), "")


class DoiFromLinksTest(unittest.TestCase):
    def test_nature_article_url(self):
        text = "原文：https://www.nature.com/articles/s41893-026-01986-6"
        self.assertEqual(doi_from_links(text), ["10.1038/s41893-026-01986-6"])

    def test_trailing_punctuation(self):
        text = "见 https://www.nature.com/articles/s41586-026-11308-7）。"
        self.assertEqual(doi_from_links(text), ["10.1038/s41586-026-11308-7"])

    def test_dedupes_and_ignores_other_hosts(self):
        text = ("https://www.nature.com/articles/s41893-026-01986-6 "
                "https://www.nature.com/articles/s41893-026-01986-6 "
                "https://example.com/articles/s41893-026-01986-6")
        self.assertEqual(doi_from_links(text), ["10.1038/s41893-026-01986-6"])

    def test_direct_doi_is_not_double_prefixed(self):
        self.assertEqual(doi_from_links("https://www.nature.com/articles/10.1038/abc"), [])

    def test_empty(self):
        self.assertEqual(doi_from_links(""), [])


class TitleSimilarityTest(unittest.TestCase):
    def test_identical(self):
        title = "Unlocking sustainable thermoelectric generation with zinc antimonide"
        self.assertEqual(title_similarity(title, title), 1.0)

    def test_case_and_punctuation(self):
        left = "Unlocking sustainable thermoelectric generation with zinc antimonide"
        right = "Unlocking Sustainable Thermoelectric Generation with Zinc Antimonide."
        self.assertGreaterEqual(title_similarity(left, right), 0.99)

    def test_different_papers_stay_below_threshold(self):
        left = "Unlocking sustainable thermoelectric generation with zinc antimonide"
        right = "Single-cell transcriptomics of the ageing mouse hypothalamus"
        self.assertLess(title_similarity(left, right), 0.5)

    def test_empty(self):
        self.assertEqual(title_similarity("", "anything"), 0.0)


class CandidateTitlesTest(unittest.TestCase):
    BODY = (
        "研究团队在《Nature Sustainability》上发表了题为"
        "“Unlocking sustainable thermoelectric generation with zinc antimonide”的论文，"
        "同期《Accounts of Chemical Research》也有一篇综述。"
        "另一项工作「Single-cell transcriptomics of the ageing mouse hypothalamus」同样值得一看。"
        "他们还提到“一种新的合成方法”，这句太短不算标题。"
    )

    def test_picks_quoted_english_titles(self):
        titles = candidate_titles(self.BODY)
        self.assertIn("Unlocking sustainable thermoelectric generation with zinc antimonide", titles)
        self.assertTrue(any(title.startswith("Single-cell transcriptomics") for title in titles))

    def test_skips_journal_names(self):
        self.assertFalse(any("Accounts of Chemical Research" in title for title in candidate_titles(self.BODY)))

    def test_skips_short_chinese_spans(self):
        self.assertFalse(any("一种新的合成方法" in title for title in candidate_titles(self.BODY)))

    def test_skips_urls(self):
        body = "“https://www.nature.com/articles/s41893-026-01986-6”"
        self.assertEqual(candidate_titles(body), [])

    def test_limit(self):
        body = "".join(f"“Paper title number {index} about thermoelectric materials”" for index in range(10))
        self.assertLessEqual(len(candidate_titles(body, limit=3)), 3)


if __name__ == "__main__":
    unittest.main()


class CitationPdfMetaTest(unittest.TestCase):
    """落地页取 PDF 地址：新刊、国内刊只能靠这条兜底。

    Vita（10.15302/vita.2026.10.0084）在 OpenAlex 查是 404、Unpaywall 没记录，
    以前直接判 closed；可官网页面一直挂着 citation_pdf_url。
    """

    def test_reads_meta_with_name_first(self):
        from app.services.papers import _citation_pdf_from_html
        html = ('<html><head><meta name="citation_pdf_url" '
                'content="https://www.vita-journal.com/vita/EN/PDF/10.15302/x">'
                '</head></html>')
        self.assertEqual(_citation_pdf_from_html(html),
                         "https://www.vita-journal.com/vita/EN/PDF/10.15302/x")

    def test_reads_meta_with_content_first(self):
        from app.services.papers import _citation_pdf_from_html
        html = ('<meta content="https://example.org/a.pdf" '
                'name="citation_pdf_url" />')
        self.assertEqual(_citation_pdf_from_html(html), "https://example.org/a.pdf")

    def test_ignores_unrelated_meta_and_missing_tag(self):
        from app.services.papers import _citation_pdf_from_html
        self.assertEqual(_citation_pdf_from_html('<meta name="og:title" content="x">'), "")
        self.assertEqual(_citation_pdf_from_html(""), "")


class ManualPaperJobPayloadTest(unittest.TestCase):
    """「让软件去找论文」「重试下载」都只是下载 PDF，不许顺手排精读。

    这是用户能直接看见的串线：点找论文，精读开始跑、点数被扣、条目状态被改。
    所以这里不测内部函数签名，直接查真正入队的载荷。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "papers.db")
        self.item_id = self.db.discover_item({
            "url": "https://journal.example/article?id=10.1000/demo.1",
            "title": "示例论文", "kind": "article",
        })[0]
        self.db.execute("""INSERT INTO papers(id,doi,title,status,created_at,updated_at)
            VALUES('p-demo','10.1000/demo.1','示例论文','failed',?,?)""",
                        (now_iso(), now_iso()))
        self.db.execute("INSERT INTO item_papers(item_id,paper_id,evidence) VALUES(?,?,?)",
                        (self.item_id, "p-demo", "test"))
        self.patch = patch("app.api.db", self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _payload(self):
        row = self.db.one("""SELECT payload_json FROM jobs WHERE kind='papers_item'
            ORDER BY created_at DESC""")
        self.assertIsNotNone(row, "应该排了一个 papers_item 任务")
        return json.loads(row["payload_json"])

    def test_find_papers_never_asks_for_analysis(self):
        from app.api import fetch_item_papers
        self.assertEqual(fetch_item_papers(self.item_id), {"queued": True})
        payload = self._payload()
        self.assertTrue(payload.get("download"))
        self.assertFalse(payload.get("analysis_after"), "找论文不该带精读开关")
        self.assertIsNone(self.db.one("SELECT id FROM jobs WHERE kind='analyze_item'"))

    def test_retry_paper_never_asks_for_analysis(self):
        from app.api import retry_paper
        retry_paper("p-demo")
        self.assertFalse(self._payload().get("analysis_after"))
        self.assertIsNone(self.db.one("SELECT id FROM jobs WHERE kind='analyze_item'"))

    def test_item_detail_calls_the_job_papers_not_archive(self):
        """状态条要说「在找论文」，不能笼统写成正在归档原文。"""
        from app.api import item_detail
        self.db.enqueue_job("papers_item",
                            {"version": 1, "item_id": self.item_id, "analysis_after": False},
                            dedupe_key=f"papers:{self.item_id}:test", priority=30)
        self.assertEqual(item_detail(self.item_id)["active_job"], "papers")
