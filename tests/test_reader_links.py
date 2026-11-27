"""离线阅读页里的外链不能就地跳转。

用户点「原始页面」时，iframe 如果直接导航到微信，微信会回「拒绝连接」，而那个
错误页在 iframe 里没有后退按钮——只能把整个阅读页叉掉。这里锁住三件事：
  1. 原始页面链接带 target="_blank"；
  2. 页面脚本拦截所有 http(s) 链接的默认跳转；
  3. 拦截后交给 parent（软件窗口），由父页面转给系统浏览器。
"""
import unittest

from app.services.archive import _reader_html


class ReaderLinkPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _reader_html("标题", "作者", "https://mp.weixin.qq.com/s/abc", "<p>正文</p>")

    def test_source_link_opens_new_window(self):
        self.assertIn('target="_blank"', self.html)
        self.assertIn('rel="noopener"', self.html)
        self.assertIn("原始页面</a>", self.html)

    def test_external_links_are_intercepted(self):
        self.assertIn("radar:open-url", self.html)
        self.assertIn("e.preventDefault();e.stopPropagation();", self.html)
        self.assertIn("window.parent&&window.parent!==window", self.html)

    def test_only_http_links_are_rerouted(self):
        # #anchor 与相对图片地址必须留在原页内，不能也被弹到浏览器。
        self.assertIn("/^https?:/i.test(href)", self.html)


if __name__ == "__main__":
    unittest.main()
