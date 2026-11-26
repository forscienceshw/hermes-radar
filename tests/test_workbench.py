"""阅读工作台的契约测试。

这里不测「双栏好不好看」——那要靠人眼和浏览器。锁的是三条一改就静默坏掉的约定：

1. 离线阅读页必须能被 iframe 嵌入。接口返回 200 不代表能嵌：只要哪天顺手加了
   `X-Frame-Options` 或 CSP 的 `frame-ancestors`，工作台就会变成一块白板。
2. 工作台骨架与「只有 ✕ 能关」的入口必须在。Esc 的 cancel 是 cancelable:false，
   靠的是 keydown 里截住默认动作；这段被删掉后，读长文按一下 Esc 就全没了。
3. 切 tab 不能用 display:none 藏 iframe——浏览器会把内部滚动位置归零，表现是
   切回原文直接跳回顶部。用 visibility 才能保住位置。
"""
import unittest
from pathlib import Path

from app.api import db, item_archive

ROOT = Path(__file__).resolve().parents[1]


class OfflinePageEmbeddableTest(unittest.TestCase):
    """离线页是工作台左栏的 iframe 源，必须允许同源嵌入。"""

    item_id = "test-workbench-embed"

    def setUp(self):
        reader = ROOT / ".tmp" / "workbench-reader.html"
        reader.parent.mkdir(parents=True, exist_ok=True)
        reader.write_text("<!doctype html><html><body><article>正文样本</article></body></html>",
                          encoding="utf-8")
        self.reader = reader
        now = "2026-11-26T00:00:00+00:00"
        db.execute(
            """INSERT OR REPLACE INTO items
               (id,kind,external_key,url,canonical_url,discovered_at,title,html_path)
               VALUES(?,?,?,?,?,?,?,?)""",
            (self.item_id, "article", self.item_id, "https://example.com/wb",
             "https://example.com/wb", now, "工作台嵌入测试", str(reader)),
        )

    def tearDown(self):
        db.execute("DELETE FROM items WHERE id=?", (self.item_id,))
        try:
            self.reader.unlink(missing_ok=True)
        except OSError:
            pass

    def test_no_header_blocks_framing(self):
        resp = item_archive(self.item_id)
        headers = {key.lower(): value for key, value in resp.headers.items()}
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("x-frame-options", headers,
                         "离线页被禁止嵌入，工作台左栏会变成白板")
        body = bytes(resp.body).decode("utf-8").lower()
        self.assertNotIn("frame-ancestors", body,
                         "CSP 的 frame-ancestors 同样会挡住 iframe 嵌入")
        self.assertIn("正文样本", body)


class WorkbenchMarkupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")

    def test_skeleton_exists(self):
        for token in ('id="workbench"', 'id="wbBody"', 'id="wbClose"',
                      'data-wb-side="reading"', 'data-wb-side="notes"', 'data-wb-side="ideas"'):
            self.assertIn(token, self.html, f"工作台骨架缺少 {token}")

    def test_only_close_button_closes(self):
        self.assertIn("$('#wbClose').onclick=()=>closeWorkbench()", self.js)
        # Esc 必须在 keydown 阶段截住；cancel 事件的 cancelable 是 false，preventDefault 无效。
        self.assertIn("e.key!=='Escape'", self.js)
        self.assertIn("e.preventDefault();e.stopPropagation()", self.js)

    def test_iframe_hidden_by_visibility(self):
        self.assertIn(".wb-view.is-active", self.css)
        self.assertNotIn(".wb-view[hidden]{display:none}", self.css,
                         "display:none 会重置 iframe 滚动位置，切回原文会跳回顶部")
        self.assertIn("classList.toggle('is-active'", self.js)

    def test_key_actions_stay_in_header(self):
        """用户点名的按钮：打开原文 / 检查原文 / 收藏到 / 删除 / 问 AI。"""
        for token in ('>打开原文<', '>检查原文<', '>收藏到…<', '>删除<', 'data-wb-ask'):
            self.assertIn(token, self.js, f"顶栏操作缺失 {token}")


if __name__ == "__main__":
    unittest.main()
