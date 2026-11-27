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
                      'data-wb-side="reading"', 'data-wb-side="paper"',
                      'data-wb-side="notes"', 'data-wb-side="ideas"'):
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
        """顶栏那一行必须能翻到所有操作，不能为了好看把功能删了。"""
        for token in ('>打开原文<', '>检查原文<', '>收藏到…<', '>重新精读<',
                      '>重新检查配图<', '>屏蔽此类<', '>删除<', 'data-wb-ask',
                      'data-wb-act="-1"', 'data-wb-act="1"', 'wb-act-track'):
            self.assertIn(token, self.js, f"顶栏操作缺失 {token}")

    def test_paper_pane_has_upload_and_fetch(self):
        for token in ('data-paper-upload', 'data-paper-fetch', '尚未下载到原论文',
                      '/api/papers/', 'paperPaneHTML'):
            self.assertIn(token, self.js, f"论文栏缺少 {token}")

    def test_action_track_pages_by_whole_buttons(self):
        """翻页要按整按钮走，并且位置留得住；平滑滚动会被重排打断，看起来像弹回原位。"""
        self.assertIn("function wbActPages(", self.js)
        self.assertIn("function wbActIndex(", self.js)
        self.assertIn("track.scrollTo({left:starts[next],behavior:'auto'})", self.js)
        self.assertNotIn("scrollBy({left:dir*", self.js, "不要再靠估算像素平移")
        # 重渲染顶栏后把位置还原，否则一刷新就回到第一页
        self.assertIn("track.scrollLeft=wb.actScroll||0", self.js)
        self.assertIn("width:min(300px,40vw)", self.css, "操作条一次只显示三四个按钮")

    def test_header_shows_running_job(self):
        """点完按钮至少顶栏能看到「正在做什么」，不能只有一句转瞬即逝的提示。"""
        self.assertIn('id="wbJob"', self.html)
        for token in ("function wbRenderJob(", "function wbJobText(", ".wb-job{"):
            self.assertIn(token, self.js if token.endswith("(") else self.css, f"缺少 {token}")
        self.assertIn("wbRenderJob(x)", self.js)

    def test_toast_lives_in_top_modal(self):
        """提示条必须挂进最上层 modal 的子树，否则被工作台的遮罩压住 = 点了没反应。"""
        self.assertIn("function toastHome(", self.js)
        self.assertIn("window.topModal&&window.topModal()", self.js)
        # 老 WebView2 没有 Popover API 时要退化成普通定位元素
        self.assertIn("typeof el.showPopover==='function'", self.js)

    def test_smart_collect_always_answers(self):
        """没匹配到也要有可见回应，并给「自己挑收藏夹」的出口。"""
        self.assertIn('id="smartCollectPick"', self.html)
        self.assertIn("这次没有对得上的收藏夹", self.js)
        self.assertIn("data&&data.already", self.js)
        self.assertIn("自己挑一个", self.js)


class ReadingModeSwitchTest(unittest.TestCase):
    """两套排布都要留着，靠设置切换。

    工作台是默认；经典弹层是改造前的界面——精读弹层 + 离线页走浏览器。
    这里锁的是「别哪天清理代码时把老界面删了」和「设置项别只画在页面上不落库」。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        from app.settings import DEFAULTS
        cls.defaults = DEFAULTS

    def test_default_is_workbench(self):
        self.assertEqual(self.defaults.get("ui", {}).get("reading_mode"), "workbench",
                         "默认排布必须是工作台；老用户升级后不会突然换成没见过的界面")

    def test_setting_control_exists(self):
        self.assertIn('id="uiReadingMode"', self.html)
        self.assertIn('value="workbench"', self.html)
        self.assertIn('value="classic"', self.html)

    def test_classic_reader_still_renders_and_opens_browser(self):
        self.assertIn("async function showItemClassic(", self.js)
        # 老界面的离线页按钮必须是新标签打开，不能悄悄换回 iframe。
        self.assertIn('/archive" target="_blank">离线阅读页<', self.js)

    def test_click_routes_through_setting(self):
        self.assertIn("if(readingMode==='classic')return showItemClassic(id,silent)", self.js)
        # 列表点击不能再直接开工作台，否则老界面形同虚设。
        self.assertIn("await showItem(t.dataset.item)", self.js)
        self.assertNotIn("await openWorkbench(t.dataset.item)", self.js)

    def test_setting_is_persisted(self):
        self.assertIn("ui:{reading_mode:$('#uiReadingMode').value}", self.js)
        self.assertIn("body:{ui:{reading_mode:next}}", self.js)

    def test_classic_opens_on_offline_page(self):
        """经典弹层也改成先看原文；只有本来就没归档正文时才落到精读。"""
        self.assertIn("classicView=x.html_path?'read':'reading'", self.js)
        self.assertIn("['read','原文'],['reading','精读'],['paper','论文']", self.js)
        self.assertIn('data-classic-view="${key}"', self.js)


class AutomationDefaultsTest(unittest.TestCase):
    """自动下载论文默认关（PDF 是磁盘大头）；自动精读默认开，但两个都能在设置里改。"""

    def test_defaults(self):
        from app.settings import DEFAULTS
        self.assertFalse(DEFAULTS["papers"]["auto_download"])
        self.assertTrue(DEFAULTS["analysis"]["auto"])

    def test_settings_controls_exist(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for token in ('id="autoAnalyze"', 'id="autoDownloadPapers"'):
            self.assertIn(token, html, f"设置页缺少 {token}")
        self.assertIn("auto:$('#autoAnalyze').value==='true'", js)
        self.assertIn("auto_download:$('#autoDownloadPapers').value==='true'", js)


if __name__ == "__main__":
    unittest.main()
