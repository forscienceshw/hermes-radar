"""联网检索的四段链路：中文匹配、多源解析、正文抓取、失败不空手。

这些断言存在的理由都是同一类事故：改动看起来无害，但让"网页结果被整批丢弃"或
"抓不到正文就整条消失"重新发生，用户看到的就是"联网搜索不好使"。
"""
import unittest

from app.services import pagefetch, websearch


class ChineseMatchTests(unittest.TestCase):
    """中文必须按 bigram 匹配，不能要求整段连续汉字原样出现。"""

    def test_grams_split_continuous_chinese(self):
        self.assertEqual(websearch._cjk_grams("纳米抗体"), ["纳米", "米抗", "抗体"])

    def test_related_chinese_title_scores_high(self):
        hit = {"title": "基于两种纳米抗体的亲和标签识别体系构建",
               "snippet": "为了发展广谱亲和纯化技术"}
        self.assertGreaterEqual(websearch._overlap(hit, "纳米抗体 亲和标签"), 0.9)

    def test_same_first_word_but_wrong_topic_is_rejected(self):
        # 搜「纳米抗体」时百度/必应会退化成只按「纳米」出结果，这些必须判低。
        hit = {"title": "纳米（长度单位）_百度百科", "snippet": "一纳米等于多少米"}
        self.assertLess(websearch._overlap(hit, "纳米抗体 亲和标签"), 0.2)

    def test_latin_and_chinese_are_scored_separately(self):
        """「PROTAC 降解剂 最新进展」里只有一个英文词，不能让它被五个 bigram 稀释。"""
        hit = {"title": "蛋白降解靶向嵌合体_百度百科", "snippet": "PROTAC 技术"}
        self.assertGreaterEqual(websearch._overlap(hit, "PROTAC 降解剂 最新进展"), 0.5)


class So360ParseTests(unittest.TestCase):
    """360 是国内网络下唯一对多词中文不降级的通用网页源。"""

    HTML = """<html><body><ul>
      <li class="res-list">
        <h3><a href="https://www.so.com/link?m=abc"
               data-mdurl="https://cdmd.cnki.com.cn/Article/x.htm">
          基于两种<em>纳米抗体</em>的<em>亲和标签</em>识别体系</a></h3>
        <p class="res-desc">为了发展广谱亲和纯化技术，本论文探索了纳米抗体的肽标签。</p>
        <p>cdmd.cnki.com.cn 反馈</p>
      </li>
      <li class="res-list">
        <h3><a href="https://www.so.com/link?m=def" data-mdurl="https://www.sohu.com/a/1">
          新一代<em>亲和</em>纯化配基</a></h3>
        <p class="res-desc">理论上纳米抗体能够识别几乎所有靶标。 www.sohu.com 反馈</p>
      </li>
    </ul></body></html>"""

    def test_real_url_comes_from_data_mdurl_not_the_redirect_shell(self):
        hits = websearch._parse_so360(self.HTML, 5)
        self.assertEqual(hits[0]["url"], "https://cdmd.cnki.com.cn/Article/x.htm")
        self.assertEqual(hits[1]["url"], "https://www.sohu.com/a/1")

    def test_em_tags_do_not_inject_spaces_into_the_title(self):
        # get_text(" ") 会把「纳米抗体」切成「纳米 抗体」，后面按词匹配就永远对不上。
        hits = websearch._parse_so360(self.HTML, 5)
        self.assertIn("纳米抗体", hits[0]["title"])

    def test_site_feedback_tail_is_stripped_from_snippet(self):
        hits = websearch._parse_so360(self.HTML, 5)
        self.assertNotIn("反馈", hits[1]["snippet"])
        self.assertIn("纳米抗体", hits[1]["snippet"])

    def test_limit_is_respected(self):
        self.assertEqual(len(websearch._parse_so360(self.HTML, 1)), 1)


class ParagraphPickTests(unittest.TestCase):
    """正文里的段落要按查询挑，不能整篇塞给模型。"""

    TEXT = (
        "导航栏这里有三十个字符左右的内容但没有任何信息量\n"
        "本研究发现纳米抗体 Nb127 只需要六个氨基酸就能以皮摩尔级亲和力结合目标蛋白，"
        "晶体结构显示结合界面位于 CDR3 环。\n"
        "今天天气很好适合出门散步，顺带聊了聊交通状况和其他一些不相干的事情，"
        "这一段是用来占位的噪声。\n"
        "定点突变实验证明第三位酪氨酸决定亲和力，突变为丙氨酸后结合能力下降约一千倍，"
        "说明该残基是识别的核心。\n"
    )

    def test_relevant_paragraphs_are_kept_and_noise_dropped(self):
        picked = pagefetch.pick_blocks(self.TEXT, ["纳米抗体", "亲和力"], 500)
        self.assertIn("六个氨基酸", picked)
        self.assertIn("酪氨酸", picked)
        self.assertNotIn("天气很好", picked)

    def test_original_order_is_preserved(self):
        """按分数挑、但按原文顺序拼：打乱顺序会让模型读出错觉的因果。"""
        picked = pagefetch.pick_blocks(self.TEXT, ["酪氨酸", "纳米抗体"], 500)
        self.assertLess(picked.index("六个氨基酸"), picked.index("酪氨酸"))

    def test_budget_is_enforced(self):
        picked = pagefetch.pick_blocks(self.TEXT * 20, ["纳米抗体"], 200)
        self.assertLessEqual(len(picked), 200)


class ReadableHostTests(unittest.TestCase):
    """知道抓不动的站点就别浪费预算，摘要照旧给模型。"""

    def test_login_walls_and_client_rendered_sites_are_skipped(self):
        for url in ("https://mp.weixin.qq.com/s/abc", "https://zhuanlan.zhihu.com/p/1",
                    "https://www.xiaohongshu.com/x", "https://weibo.com/1"):
            self.assertFalse(pagefetch.is_readable(url), url)

    def test_publisher_sites_are_skipped_but_repositories_are_not(self):
        self.assertFalse(pagefetch.is_readable("https://www.nature.com/articles/x"))
        self.assertTrue(pagefetch.is_readable("https://arxiv.org/abs/2401.00001"))
        self.assertTrue(pagefetch.is_readable("https://westlake.edu.cn/news/1"))

    def test_pdf_links_are_not_treated_as_html_pages(self):
        self.assertFalse(pagefetch.is_readable("https://example.org/paper.pdf"))


class AuthorityTests(unittest.TestCase):
    """同一个查询里，官网通知和微博热搜的字面相关度可能一样，差别在可信度。"""

    def test_social_platforms_are_downweighted(self):
        self.assertLess(websearch._authority("https://weibo.com/1"), 1.0)
        self.assertLess(websearch._authority("https://www.zhihu.com/p/1"), 1.0)

    def test_official_and_academic_domains_are_upweighted(self):
        self.assertGreater(websearch._authority("https://westlake.edu.cn/x"), 1.0)
        self.assertGreater(websearch._authority("https://www.ebi.ac.uk/x"), 1.0)


class RenderBlockTests(unittest.TestCase):
    def test_body_text_is_announced_so_model_does_not_say_summary_has_no_detail(self):
        block = websearch.render_block({
            "queries": ["纳米抗体"],
            "fetched": 2,
            "results": [{"n": 1, "title": "T", "url": "https://a.cn/1",
                         "source": "网页 · a.cn · 已读正文", "snippet": "正文段落"}],
        })
        self.assertIn("已抓取正文", block)
        self.assertIn("[1] T", block)

    def test_source_failure_is_flagged_separately_from_page_fetch_failure(self):
        """抓不到正文只是少点细节，不能说成"这个源坏了"，否则模型会以为覆盖不全。"""
        block = websearch.render_block({
            "queries": ["q"], "fetched": 0, "errors": ["Europe PMC 超过 10 秒没返回"],
            "results": [{"n": 1, "title": "T", "url": "https://a.cn/1",
                         "source": "网页 · a.cn", "snippet": "s"}],
        })
        self.assertIn("覆盖可能不完整", block)


if __name__ == "__main__":
    unittest.main()
