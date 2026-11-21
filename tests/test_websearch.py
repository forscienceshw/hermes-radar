"""联网检索的四段链路：中文匹配、多源解析、正文抓取、失败不空手。

这些断言存在的理由都是同一类事故：改动看起来无害，但让"网页结果被整批丢弃"或
"抓不到正文就整条消失"重新发生，用户看到的就是"联网搜索不好使"。
"""
import asyncio
import time
import unittest
from unittest import mock

from app import net
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

    def test_aggregator_pages_are_dropped(self):
        """360 把自家图片搜索塞进结果里；它点进去还是同一批结果，占位没意义。"""
        html = """<html><body><ul><li class="res-list">
          <h3><a data-mdurl="https://pic.360.com/search?q=x">纳米抗体 图片</a></h3>
          <p class="res-desc">海量高清图片</p></li></ul></body></html>"""
        self.assertEqual(websearch._parse_so360(html, 5), [])


class ProxyRoutingTests(unittest.TestCase):
    """只有"直连必失败"的站点才挂代理，其余一律直连。

    这条边界是"别人的电脑也能用"的支点：把 arXiv / Europe PMC 也塞进代理名单，
    一旦某台机器没有梯子，整条学术检索链就跟着废掉。
    """

    def test_only_walled_hosts_need_proxy(self):
        self.assertTrue(net.needs_proxy("https://zh.wikipedia.org/w/api.php"))
        self.assertTrue(net.needs_proxy("https://html.duckduckgo.com/html/?q=x"))
        self.assertFalse(net.needs_proxy("https://www.so.com/s?q=x"))
        self.assertFalse(net.needs_proxy("https://www.ebi.ac.uk/europepmc/webservices/rest/search"))
        self.assertFalse(net.needs_proxy("https://export.arxiv.org/api/query"))
        self.assertFalse(net.needs_proxy("https://api.crossref.org/works"))

    def test_no_proxy_means_no_proxy_argument(self):
        """没探到代理时必须原样直连，不能把空串塞进 httpx 让它崩。"""
        state = {"at": 0.0, "proxy": "", "overseas": False, "source": ""}
        with mock.patch.object(net, "_proxy_state", state):
            self.assertEqual(net._client_options("https://zh.wikipedia.org/"), {})
            self.assertEqual(net._client_options("https://www.so.com/s"), {})

    def test_missing_proxy_degrades_instead_of_raising(self):
        """一台没有梯子的电脑：探测结果是"出不去"，不是异常。"""
        state = {"at": 0.0, "proxy": "", "overseas": False, "source": ""}
        with mock.patch.object(net, "_proxy_state", state), \
                mock.patch.object(net, "_candidates", return_value=[("http://127.0.0.1:9", "本机端口")]), \
                mock.patch.object(net, "_probe_proxy", return_value=False), \
                mock.patch.object(net, "_direct_overseas", return_value=False):
            result = net.resolve_overseas(force=True)
        self.assertEqual(result["proxy"], "")
        self.assertFalse(result["overseas"])

    def test_probe_result_is_cached(self):
        """每次搜索都重新探端口会白花两秒，缓存必须真的生效。"""
        calls: list[str] = []

        def fake_probe(url: str) -> bool:
            calls.append(url)
            return True

        state = {"at": 0.0, "proxy": "", "overseas": False, "source": ""}
        with mock.patch.object(net, "_proxy_state", state), \
                mock.patch.object(net, "_candidates", return_value=[("http://127.0.0.1:7897", "本机端口")]), \
                mock.patch.object(net, "_probe_proxy", side_effect=fake_probe), \
                mock.patch.object(net, "_direct_overseas", return_value=False):
            first = net.resolve_overseas(force=True)
            second = net.resolve_overseas()
        self.assertEqual(first["proxy"], "http://127.0.0.1:7897")
        self.assertEqual(second["proxy"], "http://127.0.0.1:7897")
        self.assertEqual(len(calls), 1)


class DuckDuckGoParseTests(unittest.TestCase):
    """DDG 补的是 360/Bing 覆盖不到的英文站点，链接是跳转壳，必须还原。"""

    HTML = """<html><body>
      <div class="result results_links results_links_deep web-result">
        <a rel="nofollow" class="result__a"
           href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.nature.com%2Farticles%2Fx&amp;rut=1">
          Modeling and <b>affinity</b> maturation of a nanobody</a>
        <a class="result__snippet">We describe a nanobody affinity tag system.</a>
      </div>
      <div class="result result--ad">
        <a class="result__a" href="https://ads.example.com/buy">Buy nanobody kit</a>
      </div>
    </body></html>"""

    def test_redirect_shell_is_unwrapped_to_the_real_url(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.org%2Fb&amp;rut=1"
        self.assertEqual(websearch._unwrap_ddg(href), "https://a.org/b")

    def test_result_links_and_snippets_are_parsed(self):
        hits = websearch._parse_ddg(self.HTML, 5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["url"], "https://www.nature.com/articles/x")
        self.assertIn("nanobody", hits[0]["snippet"])

    def test_ads_are_skipped(self):
        hits = websearch._parse_ddg(self.HTML, 5)
        self.assertFalse(any("ads.example.com" in hit["url"] for hit in hits))

    def test_limit_is_respected(self):
        html = self.HTML + self.HTML.replace("nature.com/articles/x", "nature.com/articles/y")
        self.assertEqual(len(websearch._parse_ddg(html, 1)), 1)


class WikipediaTests(unittest.TestCase):
    """维基会按 UA 判定爬虫：没有联系方式就一律 403。"""

    def test_user_agent_carries_a_contact(self):
        ua = websearch.WIKI_HEADERS["User-Agent"]
        self.assertTrue("@" in ua or "http" in ua,
                        "维基 bot 策略要求 UA 里带可联系地址，否则所有请求都是 403")

    def test_rest_endpoint_is_used_not_the_blocked_generator(self):
        # generator=search 被维基的 bot 策略拦成 403，只能走 REST 搜索端点。
        # 断言参数名而不是注释：文档里提一句"不用 generator"是解释，代码里出现
        # gsrsearch 才是真的用错了端点。
        import inspect
        source = inspect.getsource(websearch.wikipedia)
        self.assertIn("rest.php/v1/search/page", source)
        self.assertNotIn("gsrsearch", source)


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


class QueryVariantTests(unittest.TestCase):
    """同一个东西换个说法，搜索引擎的覆盖完全不一样。

    「ADC」只能搜到英文综述，写「抗体药物偶联物」的中文页面一条都进不来；
    反过来纯英文的问题也搜不到中文写的经验帖。这一层不调模型，全靠查表。
    """

    def test_abbreviation_is_expanded(self):
        variants = websearch._expand_queries("ADC 偶联 最新进展", ["ADC"])
        self.assertIn("antibody-drug conjugate", variants)
        # 中文写法是「抗体药物偶联物」，中间隔着「药物」，不能用「抗体偶联」去匹配。
        self.assertTrue(any("偶联物" in item for item in variants), variants)

    def test_full_name_collapses_back_to_short_form(self):
        variants = websearch._expand_queries("nanobody affinity maturation", ["nanobody"])
        self.assertTrue(any(item.upper() == "VHH" for item in variants))

    def test_english_question_gets_a_chinese_variant(self):
        variants = websearch._expand_queries("cryo-EM structure of nanobody", ["nanobody"])
        self.assertTrue(any(websearch.CJK_RUN.search(item or "") for item in variants),
                        variants)

    def test_variants_never_repeat_the_original_query(self):
        variants = websearch._expand_queries("ADC", ["ADC"])
        self.assertNotIn("ADC", variants)

    def test_short_abbreviation_is_replaced_by_full_name_in_science_queries(self):
        # 搜「ADC 偶联」时，文献查询只留 ADC 会把模数转换器、阿拉伯裔美国人协会
        # 的论文全带进来；换成全称才落在抗体偶联药物上。
        plan = websearch.plan_queries("ADC 偶联 最新进展")
        self.assertEqual(plan["science"][0], "antibody-drug conjugate")

    def test_long_query_is_not_replaced(self):
        plan = websearch.plan_queries("nanobody affinity maturation")
        self.assertIn("nanobody", plan["science"][0].lower())


class So360BlockTests(unittest.TestCase):
    """360 被限流时返回的是 200 + 6KB 验证页，必须认出来，不能当成"没搜到"。"""

    def test_tiny_page_is_treated_as_blocked(self):
        self.assertTrue(websearch._so360_blocked("<html><title>360搜索</title></html>"))

    def test_captcha_marker_without_results_is_blocked(self):
        body = "<html>" + "x" * 25_000 + "<script>captcha()</script></html>"
        self.assertTrue(websearch._so360_blocked(body))

    def test_real_result_page_is_not_blocked(self):
        body = "<html>" + "x" * 120_000 + '<li class="res-list">ok</li></html>'
        self.assertFalse(websearch._so360_blocked(body))

    def test_cooldown_error_is_raised_instead_of_requesting_again(self):
        websearch.SO360_COOLDOWN_UNTIL = time.monotonic() + 60
        try:
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(websearch.so360("anything"))
            self.assertIn("冷却", str(caught.exception))
        finally:
            websearch.SO360_COOLDOWN_UNTIL = 0.0


class SemanticScoringTests(unittest.TestCase):
    """语义重排是加分项，不是必需项：模型不在时必须原样退回字面打分。"""

    def test_missing_model_returns_none_instead_of_raising(self):
        from app.services import embedding
        with mock.patch.object(embedding, "available", return_value=False):
            self.assertIsNone(websearch._semantic_scores([("web", "q", {"title": "t"})]))

    def test_empty_input_short_circuits(self):
        self.assertIsNone(websearch._semantic_scores([]))

    def test_semantic_floor_is_lower_for_web_than_for_papers(self):
        # 网页标题常被改写，阈值定得和文献一样高会全被扔；文献则相反。
        self.assertLess(websearch.SEMANTIC_FLOOR["web"], websearch.SEMANTIC_FLOOR["science"])


if __name__ == "__main__":
    unittest.main()
