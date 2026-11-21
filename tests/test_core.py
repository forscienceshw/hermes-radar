import asyncio
import copy
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.db import Database
from app.net import UnsafeURL, validate_public_url
from app.services.notify import queue_for_notification, quiet_delay, deliver_batch
from app.settings import _crypt, get_settings
from app.utils import canonical_url, extract_dois, extract_urls, normalize_doi

ROOT = Path(__file__).resolve().parents[1]

def _temp_root() -> Path:
    """临时库放 .tmp/：data/ 是用户资料，测试残留在那里会污染归档目录。"""
    root = ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root



class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.db = Database(Path(self.temp.name) / "test.db")
        # 这些用例测的是 3.x 那条路。通道判定会读本机装的是哪一代微信，
        # 不锁住的话，在装了 4.x 的机器上跑就全跑偏了。
        self.channel_guard = patch.dict(os.environ, {"RADAR_WECHAT_CHANNEL": "3.x"})
        self.channel_guard.start()

    def tearDown(self):
        self.channel_guard.stop()
        try:
            self.temp.cleanup()
        except BaseException:            # noqa: BLE001 - the shim raises SystemExit, not Exception
            # The host's safe-delete shim can refuse a bulk rmtree inside the repo.
            # A leftover scratch folder must not turn a passing test into an error.
            pass

    def discover(self, url="https://example.com/research?utm_source=test", title="基因编辑研究"):
        return self.db.discover_item({"url": url, "title": title, "kind": "article"})[0]

    def test_url_doi_and_share_extraction(self):
        self.assertEqual(canonical_url("https://example.com/a?utm_source=x&b=2#top"), "https://example.com/a?b=2")
        self.assertEqual(canonical_url("https://www.bilibili.com/video/BV1GJ411x7h7/?spm_id_from=333.337"), "https://www.bilibili.com/video/BV1GJ411x7h7/")
        text = "论文 https://doi.org/10.1038/NATURE12373。 视频 https://www.bilibili.com/video/BV1GJ411x7h7/"
        self.assertEqual(extract_dois(text), ["10.1038/nature12373"])
        self.assertEqual(normalize_doi("doi:10.1000/XYZ.1)"), "10.1000/xyz.1")
        self.assertEqual(len(extract_urls(text)), 2)

    def test_wechat_archive_uses_browser_identity(self):
        from types import SimpleNamespace
        from app.services import archive
        blocked = SimpleNamespace(url="https://mp.weixin.qq.com/s/abc",
                                  content=b"<html><body>environment abnormal</body></html>",
                                  headers={"content-type": "text/html; charset=utf-8"}, status=200)
        mock_request = AsyncMock(return_value=blocked)
        with patch.object(archive, "request", new=mock_request), \
                patch.object(archive, "item_directory", return_value=Path(self.temp.name) / "item"):
            with self.assertRaises(ValueError):
                asyncio.run(archive.archive_web({"id": "x", "url": "https://mp.weixin.qq.com/s/abc", "metadata": {}}))
        headers = mock_request.await_args.kwargs["headers"]
        self.assertIn("Mozilla", headers["User-Agent"])
        self.assertEqual(headers["Referer"], "https://mp.weixin.qq.com/")

    def test_discovery_is_idempotent(self):
        first = self.db.discover_item({"url": "https://example.com/a?utm_source=one", "title": "A"})
        second = self.db.discover_item({"url": "https://example.com/a?utm_source=two", "title": "A again"})
        self.assertEqual(first[0], second[0])
        self.assertTrue(first[1])
        self.assertFalse(second[1])
        self.assertEqual(self.db.stats()["items"], 1)
        self.assertEqual(self.db.stats()["jobs_pending"], 1)
        historical = {"url": "https://example.com/history", "title": "历史索引", "defer_processing": True}
        historical_id, created = self.db.discover_item(historical)
        self.assertTrue(created)
        self.assertEqual(self.db.stats()["jobs_pending"], 1)
        historical.pop("defer_processing")
        self.assertEqual(self.db.discover_item(historical), (historical_id, False))
        self.assertEqual(self.db.stats()["jobs_pending"], 2)

    def test_lease_recovery_and_dead_letter(self):
        self.db.enqueue_job("archive_item", {"version": 1, "item_id": "missing"}, dedupe_key="job-1", max_attempts=2)
        job = self.db.claim_job("test", lease_seconds=-1)
        self.assertEqual(job["state"], "LEASED")
        self.assertEqual(self.db.recover_expired_jobs(), 1)
        recovered = self.db.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
        self.assertEqual(recovered["state"], "RETRY_WAIT")
        job = self.db.claim_job("test")
        self.assertEqual(self.db.retry_job(job, "boom"), "DEAD")

    def test_chinese_full_text_search(self):
        item_id = self.discover()
        self.db.update_archive(item_id, {"title": "CRISPR 新研究", "text": "本文研究基因编辑脱靶效应与细胞修复机制。",
                                               "html_path": None, "raw_path": None, "metadata": {}})
        self.db.save_analysis(item_id, {"summary": "一种基因治疗综述", "tags": ["CRISPR"],
                                              "importance_score": 60, "relevance_score": 80, "category": "生物医学"})
        self.assertEqual(len(self.db.search("基因编辑")), 1)
        self.assertEqual(len(self.db.search("基因")), 1)
        self.assertEqual(len(self.db.search("量子引力")), 0)

    def test_batch_threshold_and_no_duplicate(self):
        settings = copy.deepcopy(get_settings())
        settings["notifications"].update({"channel": "inbox", "batch_size": 2, "max_wait_minutes": 45,
                                           "instant_threshold": 90, "min_relevance": 0, "min_interval_seconds": 0,
                                           "quiet_start": "00:00", "quiet_end": "00:00",
                                           "send_windows": [], "max_per_day": 9})
        with patch("app.services.notify.get_settings", return_value=settings):
            first, second = self.discover("https://example.com/1", "研究一"), self.discover("https://example.com/2", "研究二")
            for item_id in (first, second):
                self.db.save_analysis(item_id, {"summary": "研究", "importance_score": 50, "relevance_score": 80, "tags": []})
                queue_for_notification(self.db, item_id)
                queue_for_notification(self.db, item_id)
            batches = self.db.query("SELECT * FROM notification_batches")
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0]["state"], "CLOSED")
            self.assertEqual(self.db.one("SELECT COUNT(*) c FROM notification_items")["c"], 2)
            asyncio.run(deliver_batch(self.db, batches[0]["id"]))
            self.assertEqual(self.db.one("SELECT state FROM notification_batches WHERE id=?", (batches[0]["id"],))["state"], "SENT")
            third = self.discover("https://example.com/3", "重要研究")
            self.db.save_analysis(third, {"summary": "重要", "importance_score": 95, "relevance_score": 90, "tags": []})
            queue_for_notification(self.db, third)
            critical = self.db.one("SELECT * FROM notification_batches WHERE priority='critical'")
            self.assertEqual(critical["state"], "CLOSED")

    def test_primary_paper_ignores_figure_components(self):
        from app.services.papers import process_item_papers
        item_id = self.discover("https://journal.example/article?id=10.1371/journal.pone.0000001", "论文")
        raw = Path(self.temp.name) / "raw.html"
        raw.write_text('<meta name="citation_doi" content="10.1371/journal.pone.0000001">', encoding="utf-8")
        self.db.update_archive(item_id, {"title": "Paper", "text": "DOI 10.1371/journal.pone.0000001 and figure 10.1371/journal.pone.0000001.g001 and ref 10.1000/reference.1",
                                               "html_path": None, "raw_path": str(raw), "metadata": {}})
        with patch("app.services.papers.resolve_and_download", new=AsyncMock()):
            asyncio.run(process_item_papers(self.db, item_id, archive_only=True))
        rows = self.db.query("SELECT doi FROM papers")
        self.assertEqual([x["doi"] for x in rows], ["10.1371/journal.pone.0000001"])
        self.assertIsNone(self.db.one("SELECT id FROM jobs WHERE kind='analyze_item'"))

    def test_english_paper_rule_relevance(self):
        from app.services.analyzer import _rules_analysis
        result = _rules_analysis({"title": "A study", "text": "Abstract Methods Results. DOI 10.1000/example.1"}, get_settings())
        self.assertGreaterEqual(result["relevance_score"], 45)

    def test_llm_analysis_and_idea_pipeline(self):
        from app.services.analyzer import analyze_item
        item_id = self.discover("https://example.com/crispr", "CRISPR 研究")
        self.db.update_archive(item_id, {"title": "CRISPR 研究",
                                               "text": "研究基因编辑机制和脱靶效应，包含实验方法与结果。DOI 10.1000/crispr.1。"
                                                       + "作者在细胞系中比较了不同向导RNA的切割效率与脱靶位点，" * 60,
                                               "html_path": None, "raw_path": None, "metadata": {"archive_completeness": "full_text"}})
        idea_id = "idea-test"
        timestamp = "2026-01-01T00:00:00+00:00"
        self.db.execute("INSERT INTO ideas(id,title,content,created_at,updated_at) VALUES(?,?,?,?,?)",
                        (idea_id, "降低脱靶效应", "寻找可控的基因编辑递送策略", timestamp, timestamp))
        settings = copy.deepcopy(get_settings())
        settings["llm"]["enabled"] = True
        response = json.dumps({"summary": "严谨摘要", "relevance_score": 95, "importance_score": 92,
                               "category": "生物医学", "tags": ["CRISPR"], "critical_questions": ["如何验证？"],
                               "idea_connections": [{"idea_id": idea_id, "connection": "可检验的递送连接"}]}, ensure_ascii=False)
        with patch("app.services.analyzer.get_settings", return_value=settings), patch("app.services.analyzer._llm_call", new=AsyncMock(return_value={"content": response})):
            asyncio.run(analyze_item(self.db, item_id))
        item = self.db.one("SELECT * FROM items WHERE id=?", (item_id,))
        analysis = json.loads(item["analysis_json"])
        self.assertEqual(item["status"], "ready")
        self.assertEqual(analysis["ideas_considered"], [idea_id])
        self.assertEqual(analysis["idea_connections"][0]["idea_id"], idea_id)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM notification_items")["c"], 1)

    def test_serverchan_notification(self):
        settings = copy.deepcopy(get_settings())
        settings["notifications"].update({"channel": "serverchan", "instant_threshold": 90, "min_relevance": 0,
                                           "min_interval_seconds": 0, "quiet_start": "00:00", "quiet_end": "00:00"})
        item_id = self.discover("https://example.com/serverchan", "重要研究")
        self.db.save_analysis(item_id, {"summary": "重要发现", "importance_score": 95, "relevance_score": 90, "tags": []})
        with patch("app.services.notify.get_settings", return_value=settings), patch("app.services.notify.get_secret", return_value="https://sctapi.ftqq.com/key.send"), patch("app.services.notify.post_form", new=AsyncMock(return_value=({"code": 0}, None))) as send:
            queue_for_notification(self.db, item_id)
            batch = self.db.one("SELECT * FROM notification_batches")
            asyncio.run(deliver_batch(self.db, batch["id"]))
        self.assertEqual(send.call_count, 1)
        self.assertIn("desp", send.call_args.args[1])
        self.assertEqual(self.db.one("SELECT state FROM notification_batches")["state"], "SENT")

    def test_ollama_rejects_remote_url(self):
        from app.services.analyzer import _llm_call
        settings = copy.deepcopy(get_settings())
        settings["llm"].update({"provider": "ollama", "base_url": "https://api.deepseek.com/v1"})
        with patch("app.services.analyzer.get_settings", return_value=settings):
            with self.assertRaises(RuntimeError):
                asyncio.run(_llm_call(self.db, []))

    def test_workbuddy_provider_needs_no_api_key(self):
        from app.services.analyzer import _llm_call
        settings = copy.deepcopy(get_settings())
        settings["llm"].update({"provider": "workbuddy_cloud", "model": "hy4free",
                                # 这里测的是"配置的模型被原样传给网关"，不是自动选型；
                                # 开着 auto_cheapest 会换成当前最便宜的免费档，断言就失去意义。
                                "auto_cheapest": False})
        with patch("app.services.analyzer.get_settings", return_value=settings), patch("app.services.workbuddy_llm.call_workbuddy_full", new=AsyncMock(return_value={"content": '{"ok":true}'})) as cloud:
            result = asyncio.run(_llm_call(self.db, [{"role": "system", "content": "test"}]))
        self.assertEqual(result["content"], '{"ok":true}')
        self.assertEqual(cloud.call_args.kwargs["model"] if "model" in cloud.call_args.kwargs else cloud.call_args.args[1], "hy4free")

    def test_error_retry_classification(self):
        from app.utils import job_error_is_retryable
        self.assertTrue(job_error_is_retryable("RuntimeError: timeout connecting upstream"))
        self.assertTrue(job_error_is_retryable("HTTP 503 temporarily unavailable"))
        self.assertFalse(job_error_is_retryable("request_invalid_parameter：请求参数无效"))
        self.assertFalse(job_error_is_retryable("KeyError: 'name'"))

    def test_wecom_digest_is_compact_and_keeps_details_in_db(self):
        from app.services.notify import queue_for_notification, render_compact_wecom
        settings = copy.deepcopy(get_settings())
        settings["notifications"].update({"channel": "wecom_webhook", "instant_threshold": 80, "min_relevance": 0})
        item_id = self.discover("https://example.com/paper", "论文")
        self.db.update_archive(item_id, {"title": "一个很长的论文标题", "text": "正文",
                                               "html_path": None, "raw_path": None, "metadata": {}})
        self.db.save_analysis(item_id, {"summary": "详细" * 500, "one_sentence": "这是两分钟能读完的一句话结论。",
                                              "key_findings": [{"finding": "发现一"}, {"finding": "发现二"}],
                                              "critical_questions": ["这个追问只应保存在详细页"],
                                              "importance_score": 95, "relevance_score": 90, "tags": []})
        with patch("app.services.notify.get_settings", return_value=settings):
            queue_for_notification(self.db, item_id)
        batch = self.db.one("SELECT id FROM notification_batches")
        text = render_compact_wecom(self.db, batch["id"])
        self.assertLess(len(text), 3500)
        self.assertIn("一句话结论", text)
        self.assertNotIn("这个追问只应保存在详细页", text)
        self.assertGreater(len(self.db.one("SELECT analysis_json FROM items")["analysis_json"]), 500)

    def test_quiet_hours_overnight(self):
        settings = copy.deepcopy(get_settings())
        settings["notifications"].update({"quiet_start": "23:00", "quiet_end": "07:30", "quiet_allow_critical": True})
        night = datetime(2026, 11, 13, 23, 30).astimezone()
        self.assertGreater(quiet_delay(settings, "normal", night), 0)
        self.assertEqual(quiet_delay(settings, "critical", night), 0)
        noon = datetime(2026, 11, 13, 12, 0).astimezone()
        self.assertEqual(quiet_delay(settings, "normal", noon), 0)

    def test_ssrf_guard(self):
        for url in ("http://127.0.0.1/admin", "http://localhost/test", "file:///E:/private"):
            with self.assertRaises(UnsafeURL):
                validate_public_url(url)

    def test_wechat_self_chat_filters_cards_and_reuses_checkpoint(self):
        import lz4.block
        from app.services import wechat_self_archive as archive

        root = Path(self.temp.name) / "wechat"
        multi = root / "Msg" / "Multi"
        multi.mkdir(parents=True)
        encrypted = multi / "MSG0.db"
        encrypted.write_bytes(b"encrypted-placeholder" * 300)
        archive_dir = Path(self.temp.name) / "archive"

        def xml_card(title, url, app_type="5"):
            xml = f"<msg><appmsg><title>{title}</title><des>摘要</des><url>{url}</url><type>{app_type}</type></appmsg></msg>"
            return lz4.block.compress(xml.encode("utf-8"))

        def fake_open_snapshot(source, target, key):
            conn = sqlite3.connect(target)
            conn.execute("CREATE TABLE MSG(localId INTEGER,CreateTime INTEGER,Type INTEGER,SubType INTEGER,StrTalker TEXT,StrContent TEXT,CompressContent BLOB)")
            rows = [
                (1, 1700000000, 49, 5, "self-id", "", xml_card("公众号文章", "https://mp.weixin.qq.com/s/example?scene=1")),
                (2, 1700000001, 49, 5, "other-contact", "", xml_card("其他聊天", "https://example.com/must-not-read")),
                (3, 1700000002, 1, 0, "self-id", "视频 https://www.bilibili.com/video/BV1GJ411x7h7/?spm_id_from=x", None),
                (4, 1700000003, 49, 57, "self-id", "", xml_card("引用消息", "https://example.com/quoted", "57")),
            ]
            conn.executemany("INSERT INTO MSG VALUES(?,?,?,?,?,?,?)", rows)
            conn.commit()
            return conn

        with patch.object(archive, "ARCHIVE_DIR", archive_dir), \
                patch.object(archive, "_account_and_key", return_value=("self-id", root, "00" * 32)), \
                patch.object(archive, "_open_snapshot", side_effect=fake_open_snapshot):
            candidates, cursor = archive.sync_self_chat()
            self.assertEqual(len(candidates), 2, [item["url"] for item in candidates])
            self.assertEqual(candidates[0]["canonical_url"], "https://mp.weixin.qq.com/s/example")
            self.assertEqual(candidates[1]["kind"], "video")
            self.assertTrue(all("must-not-read" not in item["url"] for item in candidates))
            repeated, next_cursor = archive.sync_self_chat(cursor)
            self.assertEqual(repeated, [])
            self.assertEqual(next_cursor["unchanged_databases"], 1)
            # `_scratch_dir` 的 finally 会尝试清理；宿主若禁止递归删除，会留下一个
            # 只读快照。那不影响同步正确性，所以这里不断言目录已消失。

    def test_wechat_history_is_indexed_but_new_share_is_processed(self):
        from app.services import wechat_self_archive as archive
        candidates = [
            {"url": "https://example.com/old", "canonical_url": "https://example.com/old", "kind": "article",
             "published_at": "2026-01-01T00:00:00+00:00", "metadata": {}},
            {"url": "https://example.com/new", "canonical_url": "https://example.com/new", "kind": "article",
             "published_at": "2026-01-03T00:00:00+00:00", "metadata": {}},
        ]
        source = {"id": "source-id", "cursor_json": "{}",
                  "config_json": '{"history_mode":"index_only","auto_process_after":"2026-01-02T00:00:00+00:00"}'}
        with patch.object(archive, "sync_self_chat", return_value=(candidates, {"files": {}})):
            result, _ = asyncio.run(archive.poll_wechat_self(source, "test"))
        self.assertTrue(result[0]["defer_processing"])
        self.assertNotIn("defer_processing", result[1])
        self.assertTrue(all(item["source_id"] == "source-id" for item in result))

    def test_llm_prefilter_saves_credit_and_manual_force_bypasses(self):
        from app.services.analyzer import analyze_item
        item_id = self.discover("https://example.com/lifestyle", "生活随笔")
        self.db.update_archive(item_id, {"title": "生活随笔", "text": "今天记录一顿晚餐和散步，没有科研内容。" * 80,
                                               "html_path": None, "raw_path": None, "metadata": {}})
        settings = copy.deepcopy(get_settings())
        settings["llm"].update({"enabled": True, "min_local_relevance": 40})
        response = json.dumps({"summary": "手动精读", "relevance_score": 50, "importance_score": 40,
                               "category": "综合科研", "tags": []}, ensure_ascii=False)
        with patch("app.services.analyzer.get_settings", return_value=settings), \
                patch("app.services.analyzer._llm_call", new=AsyncMock(return_value={"content": response})) as cloud:
            asyncio.run(analyze_item(self.db, item_id))
            self.assertEqual(cloud.call_count, 0)
            item = self.db.one("SELECT status,analysis_json FROM items WHERE id=?", (item_id,))
            self.assertEqual(item["status"], "filtered")
            self.assertEqual(json.loads(item["analysis_json"])["analysis_mode"], "local_prefilter")
            asyncio.run(analyze_item(self.db, item_id, force=True))
            self.assertEqual(cloud.call_count, 1)
        self.assertEqual(self.db.one("SELECT status FROM items WHERE id=?", (item_id,))["status"], "ready")

    def test_public_msg_starts_at_now_then_reads_incrementally(self):
        import lz4.block
        from app.services import wechat_self_archive as archive
        root = Path(self.temp.name) / "wechat"
        (root / "Msg").mkdir(parents=True)
        (root / "Msg" / "PublicMsg.db").write_bytes(b"placeholder" + b"\0" * 8)
        rows = [(1, 1700000000, "gh_old", "", lz4.block.compress(b"<msg><appmsg><title>old</title><url>https://mp.weixin.qq.com/s/old</url><type>5</type></appmsg></msg>"))]

        def connect(*args):
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE PublicMsg(localId INTEGER,CreateTime INTEGER,StrTalker TEXT,StrContent TEXT,CompressContent BLOB,Type INTEGER DEFAULT 49,SubType INTEGER DEFAULT 5)")
            conn.executemany("INSERT INTO PublicMsg(localId,CreateTime,StrTalker,StrContent,CompressContent) VALUES(?,?,?,?,?)", rows)
            return conn

        source = {"id": "public-source", "config_json": '{"start_at_now":true,"use_catalog":false}', "cursor_json": "{}"}
        with patch.object(archive, "_account_and_key", return_value=("self", root, "00" * 32)), \
                patch.object(archive, "_open_live_readonly", side_effect=connect), \
                patch.object(archive, "_official_account_directory",
                             return_value={"gh_new": {"name": "科研公众号", "aliases": {"科研公众号"}}}):
            initial, cursor = asyncio.run(archive.poll_wechat_public_local(source, "test"))
            self.assertEqual(initial, [])
            self.assertEqual(cursor["last_local_id"], 1)
            xml = b"<msg><appmsg><title>new</title><url>https://mp.weixin.qq.com/s/new?scene=1</url><type>5</type></appmsg></msg>"
            rows.append((2, 1700000001, "gh_new", "", lz4.block.compress(xml)))
            source["cursor_json"] = json.dumps(cursor)
            candidates, next_cursor = asyncio.run(archive.poll_wechat_public_local(source, "test"))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["author"], "科研公众号")
        self.assertEqual(candidates[0]["canonical_url"], "https://mp.weixin.qq.com/s/new")
        self.assertEqual(next_cursor["last_local_id"], 2)

    def test_public_msg_catalog_name_matching(self):
        from app.services.wechat_self_archive import _public_account_filter
        directory = {
            "gh_science": {"name": "科研号", "aliases": {"科研号", "科研号备注"}},
            "gh_other": {"name": "其他号", "aliases": {"其他号"}},
        }
        accounts = [{"name": "科研号", "account_id": None}, {"name": "尚未同步号", "account_id": None}]
        with patch("app.db.Database") as database_type:
            database_type.return_value.query.return_value = accounts
            allowed, matched, unmatched = _public_account_filter({"use_catalog": True}, directory)
        self.assertEqual(allowed, {"gh_science"})
        self.assertEqual(matched, {"gh_science"})
        self.assertEqual(unmatched, 1)

    def test_research_admission_is_local_and_type_restricted(self):
        from app.services.relevance import assess_candidate
        self.assertTrue(assess_candidate({"kind": "article", "url": "https://mp.weixin.qq.com/s/a",
                                          "title": "新型基因编辑机制研究"})["keep"])
        self.assertFalse(assess_candidate({"kind": "article", "url": "https://mp.weixin.qq.com/s/b",
                                           "title": "周末美食探店"})["keep"])
        self.assertFalse(assess_candidate({"kind": "article", "url": "https://example.com/research",
                                           "title": "基因研究"})["keep"])
        self.assertTrue(assess_candidate({"kind": "video", "url": "https://b23.tv/example",
                                          "title": "量子计算实验进展"})["keep"])
        self.assertFalse(assess_candidate({"kind": "article", "url": "https://mp.weixin.qq.com/s/c",
                                           "title": "https://mp.weixin.qq.com/s/c"})["keep"])

    def test_whitelisted_account_passes_even_with_vague_title(self):
        """启用名单里的号，标题只命中泛词也要放行。

        2026-11-19「民间审稿办主任」那条「培养皿可能是你实验的最大因素」
        只命中"实验"，在泛词拦截处被静默丢弃（连 filtered 记录都没留）。
        这条放行必须排在泛词拦截之前：用户把这个号勾进追踪名单本身就是
        他要看的表态，而广告、引流、社会新闻在前面已经单独挡过一轮。
        """
        from app.services.relevance import assess_candidate
        vague = {"kind": "article", "url": "https://mp.weixin.qq.com/s?__biz=A&mid=9&idx=1",
                 "title": "有意思的工作，培养皿可能是你实验的最大因素；不过这个工作居然不去做 NRF2 上游",
                 "metadata": {"catalog_matched": True}}
        self.assertTrue(assess_candidate(vague)["keep"])
        # 同一个标题、不在名单里，还是照旧挡掉：个人口味不能借这条混过去
        vague["metadata"] = {}
        self.assertFalse(assess_candidate(vague)["keep"])
        # 名单里的广告仍然要挡
        advert = {"kind": "article", "url": "https://mp.weixin.qq.com/s?__biz=A&mid=10&idx=1",
                  "title": "招聘：实验技术员两名，待遇从优", "metadata": {"catalog_matched": True}}
        self.assertFalse(assess_candidate(advert)["keep"])

    def test_whitelisted_account_passes_when_wechat_has_no_title(self):
        """同批第二篇的标题本地没带出来，也不能挡。

        4.x 内存盒子只把第一篇的标题关联到 mid 上，idx≥2 的卡片 title 为空
        （候选里用 URL 兜底）。如果这里要求标题非空，用户勾了号的第二篇就永远
        进不了库——表现就是「同一时间推两篇，只读到上面那篇」。
        """
        from app.services.relevance import assess_candidate
        url = "https://mp.weixin.qq.com/s?__biz=A&mid=9&idx=2&sn=abc"
        whitelisted = {"kind": "article", "url": url, "title": url,
                       "metadata": {"catalog_matched": True}}
        self.assertTrue(assess_candidate(whitelisted)["keep"])
        # 名单外的号还是不能靠一条裸链接混进来
        self.assertFalse(assess_candidate({"kind": "article", "url": url, "title": url,
                                           "metadata": {}})["keep"])

    def test_archive_preserves_original_share_metadata(self):
        item_id = self.db.discover_item({"url": "https://www.bilibili.com/video/BV1GJ411x7h7/",
                                         "kind": "video", "title": "科研视频",
                                         "metadata": {"original_share_url": "https://b23.tv/original",
                                                      "share_channel": "wechat_self_chat"}})[0]
        self.db.update_archive(item_id, {"title": "科研视频", "text": "字幕", "metadata": {"bvid": "BV1GJ411x7h7"}})
        metadata = json.loads(self.db.one("SELECT metadata_json FROM items WHERE id=?", (item_id,))["metadata_json"])
        self.assertEqual(metadata["original_share_url"], "https://b23.tv/original")
        self.assertEqual(metadata["share_channel"], "wechat_self_chat")
        self.assertEqual(metadata["bvid"], "BV1GJ411x7h7")

    def test_excluded_items_are_hidden_restorable_and_not_processed(self):
        item_id = self.discover()
        self.assertEqual(self.db.stats()["jobs_pending"], 1)
        self.assertEqual(self.db.set_items_excluded([item_id], "测试过滤"), 1)
        self.assertEqual(self.db.stats()["items"], 0)
        self.assertEqual(self.db.stats()["excluded"], 1)
        self.assertEqual(self.db.search(), [])
        self.assertEqual(self.db.stats()["jobs_pending"], 0)
        self.db.update_archive(item_id, {"title": "不应写回", "text": "不应写回", "metadata": {}})
        self.assertNotEqual(self.db.one("SELECT status FROM items WHERE id=?", (item_id,))["status"], "archived")
        self.assertEqual(self.db.set_items_excluded([item_id], "恢复", excluded=False), 1)
        self.assertEqual(self.db.stats()["items"], 1)

    def test_offline_wechat_root_relocation_uses_verified_candidate(self):
        from app.services import wechat_self_archive as archive
        old = Path(self.temp.name) / "old"
        new = Path(self.temp.name) / "new"
        new.mkdir()
        with patch.object(archive, "_offline_root_candidates", return_value=[old, new]), \
                patch.object(archive, "_key_is_valid", side_effect=lambda root, key: root == new):
            self.assertEqual(archive._locate_offline_root("self", str(old), "key"), new)

    def test_local_sources_activate_once_after_restart(self):
        from app.worker import activate_pending_sources
        timestamp = datetime.now().astimezone().isoformat()
        self.db.execute("""INSERT INTO sources(id,type,name,config_json,enabled,poll_seconds,created_at,updated_at)
            VALUES(?,?,?,?,0,30,?,?)""", ("local-test", "wechat_public_local", "本地公众号",
                                         '{"enable_after_restart":true,"start_at_now":true}', timestamp, timestamp))
        self.assertEqual(activate_pending_sources(self.db), 1)
        source = self.db.one("SELECT enabled,config_json FROM sources WHERE id='local-test'")
        self.assertEqual(source["enabled"], 1)
        self.assertNotIn("enable_after_restart", json.loads(source["config_json"]))
        self.assertEqual(activate_pending_sources(self.db), 0)

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_dpapi_round_trip(self):
        value = "测试密钥-123"
        self.assertEqual(_crypt(_crypt(value.encode(), True), False).decode(), value)


    def test_short_source_is_marked_unreadable_instead_of_guessed(self):
        from app.services.analyzer import analyze_item
        item_id = self.discover("https://example.com/brief", "一条简讯")
        self.db.update_archive(item_id, {"title": "一条简讯", "text": "某团队报道了一种新材料。",
                                               "html_path": None, "raw_path": None, "metadata": {}})
        settings = copy.deepcopy(get_settings())
        settings["llm"]["enabled"] = True
        with patch("app.services.analyzer.get_settings", return_value=settings), \
                patch("app.services.analyzer._llm_call", new=AsyncMock(return_value={"content": "{}"})) as cloud:
            asyncio.run(analyze_item(self.db, item_id, force=True))
        row = self.db.one("SELECT status,analysis_json FROM items WHERE id=?", (item_id,))
        self.assertEqual(row["status"], "insufficient")
        self.assertEqual(cloud.call_count, 0)
        self.assertIn("无法详细归档", json.loads(row["analysis_json"])["summary"])

    def test_stale_failure_rows_are_pruned_after_success(self):
        item_id = self.discover("https://example.com/prune", "会被重读的条目")
        timestamp = datetime.now().astimezone().isoformat()
        self.db.enqueue_job("analyze_item", {"version": 1, "item_id": item_id},
                            dedupe_key=f"analyze:{item_id}:dead", max_attempts=1)
        job = self.db.one("SELECT id FROM jobs WHERE dedupe_key=?", (f"analyze:{item_id}:dead",))
        self.db.execute("UPDATE jobs SET state='DEAD',last_error='TimeoutError: 超时',updated_at=? WHERE id=?",
                        (timestamp, job["id"]))
        self.assertEqual(self.db.prune_stale_jobs(), 0)
        self.db.save_analysis(item_id, {"summary": "后来成功了", "relevance_score": 60, "importance_score": 50}, "ready")
        self.assertGreaterEqual(self.db.prune_stale_jobs(), 1)
        self.assertIsNone(self.db.one("SELECT id FROM jobs WHERE dedupe_key=?", (f"analyze:{item_id}:dead",)))

    def test_prune_keeps_only_newest_poll_failure_per_source(self):
        """采集任务每轮都有新 dedupe_key，失败一次留一条死信，一天能堆几百条。

        同一个来源只该留最新一条当证据——真正的原因在 sources.last_error 里。
        """
        for round_no in range(4):
            self.db.enqueue_job("poll_source", {"version": 1, "source_id": "src-1", "round": round_no},
                                dedupe_key=f"poll:src-1:{round_no}")
        self.db.enqueue_job("poll_source", {"version": 1, "source_id": "src-2", "round": 0},
                            dedupe_key="poll:src-2:0")
        self.db.execute("""UPDATE jobs SET state='DEAD',last_error='RuntimeError: 未能识别当前微信账号',
            updated_at='2026-11-19T00:00:00+00:00' WHERE kind='poll_source'""")
        self.db.prune_stale_jobs()
        rows = self.db.query("SELECT payload_json FROM jobs WHERE kind='poll_source' AND state='DEAD'")
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum('src-1' in row["payload_json"] for row in rows), 1)
        self.assertEqual(sum('src-2' in row["payload_json"] for row in rows), 1)

    def test_environmental_errors_are_classified(self):
        from app.utils import job_error_is_environmental
        self.assertTrue(job_error_is_environmental("RuntimeError: 未能识别当前微信账号，请先打开并登录微信"))
        self.assertTrue(job_error_is_environmental(
            "RuntimeError: 检测到微信 4.x 的 Weixin.exe；当前只读适配器不会复用 3.x 密钥读取新版本数据"))
        self.assertFalse(job_error_is_environmental("ValueError: 没有提取到有效正文"))
        self.assertFalse(job_error_is_environmental(""))

    def test_reader_sanitizer_unhides_wechat_content_root(self):
        from bs4 import BeautifulSoup
        from app.services.archive import _sanitize
        soup = BeautifulSoup('<div id="js_content" style="visibility: hidden; opacity: 0; "><p>正文</p></div>', "lxml")
        root = soup.select_one("#js_content")
        _sanitize(root)
        self.assertNotIn("style", root.attrs)
        self.assertIn("正文", str(root))

    def test_figure_cards_pair_captions_with_archived_images(self):
        from app.services.analyzer import figure_cards
        # 图注是独立段落；正文里的"(图7A)"只是引用，不能被当成图注挂在图下面。
        item = {"text": "扫描结果见正文（图7A），这里先看装置。\n图 1　装置示意\n图 2　测量结果曲线",
                "metadata_json": json.dumps({
                    "images": [{"index": 0, "local_path": "a.png"}, {"index": 1, "local_path": "b.png"}]})}
        cards = figure_cards(item)
        self.assertEqual([c["number"] for c in cards], [1, 2])
        self.assertEqual(cards[0]["caption"], "装置示意")
        self.assertEqual(cards[1]["caption"], "测量结果曲线")

    def test_long_figure_caption_is_not_cut_at_ninety_characters(self):
        from app.services.analyzer import figure_cards
        long_caption = "单细胞测序揭示的细胞类型分布，" * 8
        item = {"text": "图1　" + long_caption,
                "metadata_json": json.dumps({"images": [{"index": 0, "local_path": "a.png"}]})}
        cards = figure_cards(item)
        self.assertEqual(cards[0]["caption"], long_caption.strip().rstrip("，"))


    def test_credit_ledger_uses_actually_charged_points(self):
        from app.services import credits
        rows = [
            {"time": "2026-01-01T00:00:00+00:00", "model": "paid", "task": "analyze", "elapsed_ms": 1000,
             "usage": {"credit": 0.4, "original_credit": 0, "total_tokens": 100}},
            {"time": "2026-01-02T00:00:00+00:00", "model": "free", "task": "analyze", "elapsed_ms": 2000,
             "usage": {"credit": 0, "original_credit": 7.5, "total_tokens": 900}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
            path = Path(handle.name)
        original = credits.USAGE_LOG
        credits.USAGE_LOG = path
        try:
            data = credits.summary(3650)
            recent = credits.recent_calls(5)
        finally:
            credits.USAGE_LOG = original
            path.unlink()
        self.assertEqual(data["total_points"], 0.4)
        # 付费行没给 original_credit 时，原价按实扣算（0.4）；免费行那 7.5 才是目录原价。
        # 旧实现把付费行的原价记成 0，界面上会出现"实扣 0.4 / 原价 0"这种怪数字。
        self.assertEqual(data["total_reference"], 7.9)
        self.assertEqual(data["reading_avg"], 0.2)
        self.assertEqual(recent[0]["model"], "free")

    def test_item_context_carries_article_and_selection(self):
        from app.services.assistant import item_context
        item_id = self.discover("https://example.com/ctx", "上下文测试")
        self.db.update_archive(item_id, {"title": "冷冻电镜解析的复合物", "text": "正文：" + "作者测量了构象变化。" * 40,
                                               "html_path": None, "raw_path": None, "metadata": {}})
        self.db.save_analysis(item_id, {"hook": "一句话结论", "steps": [{"title": "第一步", "did": "做了什么"}],
                                              "answer": "结论内容", "importance_score": 60, "relevance_score": 70})
        text = item_context(self.db, item_id, selection="我选中的这一段")
        self.assertIn("冷冻电镜解析的复合物", text)
        self.assertIn("一句话结论", text)
        self.assertIn("我选中的这一段", text)

    def test_note_roundtrip(self):
        item_id = self.discover("https://example.com/note", "笔记来源")
        timestamp = datetime.now().astimezone().isoformat()
        self.db.execute("""INSERT INTO notes(id,item_id,section,quote,body,tags_json,pinned,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", ("n1", item_id, "answer", "被引用的原文", "我的想法", "[]", 0, timestamp, timestamp))
        row = self.db.one("SELECT quote,body FROM notes WHERE id=?", ("n1",))
        self.assertEqual(row["quote"], "被引用的原文")
        self.db.execute("UPDATE notes SET pinned=1 WHERE id=?", ("n1",))
        self.assertEqual(self.db.one("SELECT pinned FROM notes WHERE id=?", ("n1",))["pinned"], 1)

    def test_digest_separates_focus_topics_from_the_rest(self):
        from app.services.digest import focus_tags
        self.assertIn("结构生物学", focus_tags({"title": "冷冻电镜解析核孔复合物构象"}, {}))
        self.assertIn("RNA 从头合成", focus_tags({"title": "无需模板从头合成随机 RNA 链的酶"}, {}))
        self.assertIn("适配体与传感器", focus_tags({"title": "基于适配体的电化学传感器检测限"}, {}))
        self.assertEqual(focus_tags({"title": "某基因在肺癌细胞系中的表达"}, {}), [])

    def test_history_links_before_cutoff_are_skipped(self):
        from app.services.wechat_self_archive import poll_wechat_self
        def fake_sync(cursor, wxid, name):
            return [
                {"url": "https://mp.weixin.qq.com/s/old", "canonical_url": "https://mp.weixin.qq.com/s/old",
                 "external_key": "old", "kind": "article", "title": "旧链接",
                 "published_at": "2020-01-01T00:00:00+00:00", "metadata": {}},
                {"url": "https://mp.weixin.qq.com/s/new", "canonical_url": "https://mp.weixin.qq.com/s/new",
                 "external_key": "new", "kind": "article", "title": "新链接",
                 "published_at": "2030-01-01T00:00:00+00:00", "metadata": {}},
            ], {}
        source = {"id": "s1", "config_json": json.dumps({"history_mode": "skip",
                                                          "auto_process_after": "2026-01-01T00:00:00+00:00"})}
        with patch("app.services.wechat_self_archive.sync_self_chat", new=fake_sync):
            kept, cursor = asyncio.run(poll_wechat_self(source, ""))
        self.assertEqual([x["title"] for x in kept], ["新链接"])
        self.assertEqual(cursor["skipped_history"], 1)


    def test_multi_term_search_and_short_chinese_query(self):
        item_id = self.discover(url="https://example.com/a?utm_source=x", title="只要 6 个氨基酸：纳米抗体把亲和标签减到极简")
        self.db.update_archive(item_id, {"title": "只要 6 个氨基酸：纳米抗体把亲和标签减到极简",
                                         "text": "Nb127 只用了六个残基就完成了识别。",
                                         "html_path": None, "raw_path": None, "metadata": {}})
        self.assertEqual([x["id"] for x in self.db.search("纳米抗体")], [item_id])
        self.assertEqual([x["id"] for x in self.db.search("纳米抗体 亲和标签")], [item_id])
        self.assertEqual(self.db.search_count("纳米抗体 亲和标签"), 1)
        self.assertEqual(self.db.search("纳米抗体 量子引力"), [])

    def test_wechat_share_parameters_collapse_to_one_item(self):
        from app.utils import canonical_url as canon
        a = canon("http://mp.weixin.qq.com/s?__biz=MzA5&mid=224&idx=1&sn=abc&chksm=1&scene=0#rd")
        b = canon("https://mp.weixin.qq.com/s?__biz=MzA5&mid=224&idx=1&sn=abc&mpshare=1&srcid=9#rd")
        c = canon("https://mp.weixin.qq.com/s?__biz=MzA5&mid=224&idx=2&sn=abc&chksm=2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, "https://mp.weixin.qq.com/s?__biz=MzA5&mid=224&idx=1")

    def test_social_news_and_case_notices_stay_out_of_the_library(self):
        from app.services.relevance import assess_candidate
        news = assess_candidate({
            "kind": "article", "url": "https://mp.weixin.qq.com/s?__biz=A&mid=3&idx=1",
            "title": "河南一高校再回应“教师性骚扰女生”：予以开除｜一周动态"})
        self.assertFalse(news["keep"])
        self.assertIn("社会新闻", news["reason"])
        # 科研标题里的"自杀毒素"是细菌机制，不能因为"自杀"两个字被挡掉
        science = assess_candidate({
            "kind": "article", "url": "https://mp.weixin.qq.com/s?__biz=A&mid=4&idx=1",
            "title": "细菌自杀毒素的结构解析与抗菌机制"})
        self.assertTrue(science["keep"])

    def test_ad_words_in_body_no_longer_exclude_research(self):
        from app.services.relevance import assess_candidate
        keep = assess_candidate({"kind": "article", "url": "https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1",
                                 "title": "DNA 支架里的局部拥挤效应，解锁不对称三组分反应",
                                 "metadata": {"description": "Angewandte Chemie 论文解读"},
                                 "text": "把这套思路推广到其他反应类型，是作者提出的延伸方向。"},
                                include_archived_text=True)
        self.assertTrue(keep["keep"])
        drop = assess_candidate({"kind": "article", "url": "https://mp.weixin.qq.com/s?__biz=A&mid=2&idx=1",
                                 "title": "招聘：实验室科研助理两名", "metadata": {}})
        self.assertFalse(drop["keep"])

    def test_collections_roundtrip_and_filter(self):
        item_id = self.discover(url="https://example.com/collect", title="基因编辑收藏测试")
        folder = self.db.collection_create("经典推文", "library")
        self.db.collection_toggle(folder["id"], "item", item_id, True)
        self.assertEqual(self.db.collection_entry_ids(folder["id"], "item"), [item_id])
        self.assertEqual(self.db.collection_ids_for("item", [item_id])[item_id], [folder["id"]])
        self.assertEqual([x["id"] for x in self.db.search(collection=folder["id"])], [item_id])
        self.assertEqual([x["id"] for x in self.db.search(collection="__none__")], [])
        self.db.collection_toggle(folder["id"], "item", item_id, False)
        self.assertEqual(self.db.collection_entry_ids(folder["id"], "item"), [])
        self.assertEqual(self.db.collection_delete(folder["id"]), 1)

    def test_search_query_planning_keeps_chinese_out_of_literature_apis(self):
        from app.services.websearch import plan_queries, render_block
        plan = plan_queries("这个纳米抗体为什么只要6个氨基酸就能结合",
                            "只要 6 个氨基酸：纳米抗体 Nb127")
        # 中文长句进 Europe PMC 只会匹配到零散汉字，必须翻译成英文术语再查。
        self.assertTrue(plan["science"])
        for query in plan["science"]:
            self.assertIsNone(re.search(r"[\u4e00-\u9fff]", query), query)
        self.assertIn("nanobody", " ".join(plan["science"]).lower())
        self.assertEqual(plan["web"], "纳米抗体 只要6个氨基酸就能结合")
        block = render_block({"results": [{"n": 1, "title": "A paper", "url": "https://doi.org/10.1/x",
                                           "source": "Europe PMC · Nature", "snippet": "摘要"}]})
        self.assertIn("[1] A paper", block)
        self.assertIn("https://doi.org/10.1/x", block)

    def test_english_question_is_sent_to_literature_apis_directly(self):
        from app.services.websearch import plan_queries
        plan = plan_queries("What does AlphaFold3 do for protein-ligand complexes?")
        self.assertTrue(any("AlphaFold3" in q for q in plan["science"]))

    def test_followup_question_borrows_article_topics(self):
        """追问没有主语（"它用了哪种偶联化学"），查询里必须补上文章的名词。"""
        from app.services.websearch import plan_queries
        terms = ["泛素化", "E1/E2/E3", "酰基-酶中间体", "MESNa活化泛素"]
        plan = plan_queries("帮我搜一下是具体用了哪种偶联化学",
                            "David Baker《bioRxiv》：利用RFD3首次从头设计泛素连接酶", terms)
        self.assertIn("泛素化", plan["web"])
        self.assertNotIn("帮我搜", plan["web"])
        self.assertEqual(plan["science"], ["ubiquitin conjugation"])

    def test_bing_rss_results_are_parsed_and_filtered(self):
        from app.services.websearch import _parse_bing_rss, render_block
        xml = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
            <title>必应：ubiquitin</title>
            <item><title>Ubiquitin conjugation chemistry</title>
              <link>https://www.nature.com/articles/abc</link>
              <description>&lt;p&gt;Conjugation of ubiquitin to lysine.&lt;/p&gt;</description>
              <pubDate>Sat, 15 Nov 2026 08:00:00 GMT</pubDate></item>
            <item><title>Ubiquitin conjugation review</title>
              <link>https://www.nature.com/articles/abc</link>
              <description>duplicate link</description></item>
        </channel></rss>"""
        hits = _parse_bing_rss(xml, 5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["url"], "https://www.nature.com/articles/abc")
        self.assertIn("Conjugation of ubiquitin", hits[0]["snippet"])
        self.assertTrue(hits[0]["source"].startswith("网页 · nature.com"))

    def test_promo_course_and_vague_titles_are_not_admitted(self):
        """招生、课程、只有"研究/发现"泛词的内容，入库前就该挡掉，别占磁盘。"""
        from app.services.relevance import assess_candidate
        base = {"kind": "article", "url": "https://mp.weixin.qq.com/s/abc", "author": "某号"}
        cases = [
            ("西湖大学2027年生物学博士生招生宣讲", "细胞 蛋白 基因 实验", False),
            ("第 2 课｜握拍之道：正反手握拍", "实验 数据分析 论文", False),
            ("家长发现女儿有打拳倾向", "研究发现", False),
            ("Nat Commun | 团队发现pPVT抗痒环路", "", True),
            ("化学合成路线是干什么的", "泛素 偶联化学", True),
        ]
        for title, description, expected in cases:
            verdict = assess_candidate({**base, "title": title,
                                        "metadata": {"description": description}})
            self.assertEqual(verdict["keep"], expected, f"{title} -> {verdict}")

    def test_empty_search_still_tells_the_model_it_searched(self):
        """没有结果也不能让模型说"我无法联网"：必须留下"搜过了但没结果"。"""
        from app.services.websearch import render_block
        block = render_block({"queries": ["泛素化 偶联化学"], "results": [],
                              "errors": ["Bing 返回的结果不相干，已全部丢弃"]})
        self.assertIn("泛素化 偶联化学", block)
        self.assertIn("没检索到", block)
        self.assertIn("不要说", block)
        self.assertEqual(render_block({"queries": [], "results": []}), "")

    def test_context_window_guess_and_token_estimate(self):
        from app.services.assistant import context_window, estimate_tokens
        self.assertEqual(context_window("deepseek-v4.1-flash"), 64000)
        self.assertEqual(context_window("glm-5.3-flash"), 128000)
        self.assertEqual(context_window("mystery-model"), 32000)
        self.assertEqual(context_window("mystery-model", configured=200000), 200000)
        self.assertGreater(estimate_tokens("中文" * 500), estimate_tokens("a" * 500))

    def test_long_conversation_folds_old_turns_instead_of_failing(self):
        from app.services.assistant import fit_messages
        system = "系统提示" * 200
        history = []
        for index in range(20):
            history.append({"role": "user", "content": f"第{index}问：" + "问" * 60})
            history.append({"role": "assistant", "content": f"第{index}答：" + "答" * 220})
        tight, info = fit_messages(system, history, "新的问题", budget=3000)
        self.assertLessEqual(info["tokens"], 3000)
        self.assertGreater(info["folded"], 0)
        self.assertIn("更早的对话", tight[0]["content"])
        self.assertEqual(tight[-1]["content"], "新的问题")
        self.assertEqual(tight[-2]["content"], history[-1]["content"])
        # 预算充足时不动历史。
        roomy, info2 = fit_messages(system, history, "新的问题", budget=40000)
        self.assertEqual(info2["folded"], 0)
        self.assertEqual(len(roomy), 1 + len(history) + 1)

    def test_build_messages_shrinks_body_before_dropping_history(self):
        from app.services.assistant import build_messages
        item_id = self.discover("https://example.com/budget", "预算测试")
        self.db.update_archive(item_id, {"title": "长文测试", "text": "作者测量了构象变化。" * 3000,
                                         "html_path": None, "raw_path": None, "metadata": {}})
        chat = self.db.one("SELECT id FROM chats ORDER BY created_at DESC")
        if not chat:
            from app.services.assistant import ensure_chat
            chat = ensure_chat(self.db, None, title="预算对话")
        messages, info = build_messages(self.db, chat["id"], "核心结论是什么？",
                                        item_id=item_id, model="deepseek-v4.1-flash")
        self.assertEqual(info["window"], 64000)
        self.assertLessEqual(info["tokens"], info["budget"])
        self.assertEqual(messages[-1]["role"], "user")
        # 窗口很小时应当先压缩正文，而不是直接报错。
        _, small = build_messages(self.db, chat["id"], "核心结论是什么？", item_id=item_id,
                                  model="yi-large", configured_window=6000)
        self.assertLess(small["body_limit"], 14000)
        self.assertLessEqual(small["tokens"], small["budget"])

    def test_next_window_never_returns_the_slot_it_is_standing_in(self):
        from app.services.digest import next_window
        settings = get_settings()
        # 20:31 已经过了 20:30 那班；旧实现返回 20:30，算出 1 秒延迟后无限自旋。
        delay, target = next_window(settings, datetime(2026, 11, 15, 20, 31))
        self.assertGreater(delay, 3600)
        self.assertEqual(target.strftime("%m-%d %H:%M"), "11-16 08:30")
        delay, target = next_window(settings, datetime(2026, 11, 15, 17, 9))
        self.assertEqual(target.strftime("%H:%M"), "20:30")
        self.assertGreater(delay, 60)

    def test_closed_batches_merge_into_one_pending_digest(self):
        from app.services.notify import merge_closed_batches
        ids = []
        for index in range(3):
            item_id = self.discover(f"https://example.com/merge-{index}", f"合并测试 {index}")
            self.db.save_analysis(item_id, {"summary": "摘要", "relevance_score": 60, "importance_score": 60}, "ready")
            batch_id = f"batch-{index}"
            self.db.execute("""INSERT INTO notification_batches(id,state,priority,created_at,deadline,delivery_id,channel)
                VALUES(?,'CLOSED','normal','2026-11-15T00:00:00+00:00','2026-11-15T00:00:00+00:00',?,'inbox')""",
                            (batch_id, f"delivery-{batch_id}"))
            self.db.execute("""INSERT INTO notification_items(item_id,batch_id,score,reason,created_at)
                VALUES(?,?,?,?,?)""", (item_id, batch_id, 60, "普通内容积攒汇报", "2026-11-15T00:00:00+00:00"))
            self.db.enqueue_job("deliver_batch", {"version": 1, "batch_id": batch_id},
                                dedupe_key=f"deliver:{batch_id}", priority=25, max_attempts=8)
            self.db.execute("UPDATE jobs SET state='RETRY_WAIT',attempts=904 WHERE dedupe_key=?",
                            (f"deliver:{batch_id}",))
            ids.append(batch_id)
        keep = merge_closed_batches(self.db, keep_id=ids[-1])
        self.assertEqual(keep, ids[-1])
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM notification_batches WHERE state='CLOSED'")["c"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM notification_batches WHERE state='MERGED'")["c"], 2)
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM notification_items WHERE batch_id=?", (keep,))["c"], 3)
        pending = self.db.query("SELECT attempts,state FROM jobs WHERE kind='deliver_batch' AND state IN ('READY','RETRY_WAIT')")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 0)

    def test_prune_drops_cancelled_delivery_jobs_of_merged_batches(self):
        # The merged batches left 900-attempt zombies behind; they were cancelled and
        # can never run again, so keeping them only re-created the look of a backlog.
        item_id = self.discover("https://example.com/zombie", "僵尸任务测试")
        self.db.save_analysis(item_id, {"summary": "摘要", "relevance_score": 60, "importance_score": 60}, "ready")
        self.db.execute("""INSERT INTO notification_batches(id,state,priority,created_at,deadline,delivery_id,channel)
            VALUES('zombie','MERGED','normal','2026-11-15T00:00:00+00:00','2026-11-15T00:00:00+00:00','d-zombie','inbox')""")
        self.db.enqueue_job("deliver_batch", {"version": 1, "batch_id": "zombie"},
                            dedupe_key="deliver:zombie", priority=25)
        self.db.execute("UPDATE jobs SET state='CANCELLED',attempts=903 WHERE dedupe_key='deliver:zombie'")
        self.db.prune_stale_jobs()
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM jobs WHERE dedupe_key='deliver:zombie'")["c"], 0)

    def test_deleted_source_is_dropped_only_without_an_offline_copy(self):
        from app.pipeline import JobRunner
        runner = JobRunner(self.db)
        bare = self.discover("https://example.com/gone", "被删除且没存离线页")
        runner._source_gone(bare, "发布者已删除这篇推文")
        row = self.db.one("SELECT excluded_at,exclusion_reason FROM items WHERE id=?", (bare,))
        self.assertIsNotNone(row["excluded_at"])
        self.assertIn("没有离线页", row["exclusion_reason"])

        kept = self.discover("https://example.com/gone-but-archived", "被删除但已存离线页")
        page = Path(self.temp.name) / "reader.html"
        page.write_text("<html>正文</html>", encoding="utf-8")
        self.db.update_archive(kept, {"title": "被删除但已存离线页", "text": "正文" * 200,
                                      "html_path": str(page), "raw_path": None, "metadata": {}})
        runner._source_gone(kept, "发布者已删除这篇推文")
        row = self.db.one("SELECT excluded_at,metadata_json FROM items WHERE id=?", (kept,))
        self.assertIsNone(row["excluded_at"])
        self.assertEqual(json.loads(row["metadata_json"])["source_status"]["state"], "deleted")

    def test_page_furniture_images_never_reach_the_blob_store(self):
        from bs4 import BeautifulSoup
        from app.services.archive import junk_image_reason, tiny_image_reason
        soup = BeautifulSoup('<img class="qr_code_pc_img" alt="二维码"><img class="rich_pages wxw-img" data-w="832" alt="">', "lxml")
        qr, figure = soup.find_all("img")
        self.assertIn("页面元素", junk_image_reason(qr, "https://mmbiz.qpic.cn/x/640"))
        self.assertEqual(junk_image_reason(figure, "https://mmbiz.qpic.cn/x/640"), "")
        import io as _io
        from PIL import Image
        small = _io.BytesIO()
        Image.new("RGB", (40, 40), "white").save(small, "PNG")
        self.assertIn("尺寸过小", tiny_image_reason(small.getvalue()))
        large = _io.BytesIO()
        canvas = Image.new("RGB", (800, 600), "white")
        canvas.paste(Image.new("RGB", (400, 300), "black"), (50, 50))
        canvas.save(large, "PNG")
        self.assertEqual(tiny_image_reason(large.getvalue()), "")
        # 微信懒加载失败的整页白底占位图：尺寸正常、体积不小，但没有任何像素变化。
        blank = _io.BytesIO()
        Image.new("RGB", (710, 300), "white").save(blank, "PNG")
        self.assertIn("纯色空白图", tiny_image_reason(blank.getvalue()))

    def test_dropped_images_leave_metadata_and_reader(self):
        from app.services.archive import drop_archived_images
        item_id = self.discover("https://example.com/image-triage", "图片分诊")
        folder = Path(self.temp.name) / "doc"
        folder.mkdir()
        blob_dir = Path(self.temp.name) / "blobs" / "ab"
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "abcdef.png"
        blob.write_bytes(b"x")
        page = folder / "reader.html"
        page.write_text('<article><img src="../../blobs/ab/abcdef.png"><img src="../../blobs/ab/keep.png"></article>',
                        encoding="utf-8")
        self.db.update_archive(item_id, {"title": "图片分诊", "text": "正文" * 200, "html_path": str(page),
                                         "raw_path": None, "metadata": {"images": [
                                             {"index": 0, "local_path": str(blob), "relative_path": "../../blobs/ab/abcdef.png"},
                                             {"index": 1, "local_path": str(blob_dir / "keep.png"), "relative_path": "../../blobs/ab/keep.png"},
                                         ]}})
        removed = drop_archived_images(self.db, item_id, [0], "广告卡片")
        self.assertEqual(removed, 1)
        metadata = json.loads(self.db.one("SELECT metadata_json FROM items WHERE id=?", (item_id,))["metadata_json"])
        self.assertEqual([image["index"] for image in metadata["images"]], [1])
        self.assertEqual(metadata["image_triage"]["dropped"], 1)
        html = page.read_text(encoding="utf-8")
        self.assertNotIn("abcdef.png", html)
        self.assertIn("keep.png", html)
        self.assertFalse(blob.exists())


    def test_image_triage_drops_page_one_banners_even_when_the_article_says_tu1(self):
        from PIL import Image
        from app.services import analyzer
        item_id = self.discover("https://example.com/triage", "视觉分诊")
        folder = Path(self.temp.name) / "triage-doc"
        folder.mkdir()
        blobs = []
        for index in range(3):
            path = Path(self.temp.name) / f"shot-{index}.png"
            Image.new("RGB", (300, 200), (30 + index * 20, 60, 90)).save(path)
            blobs.append(path)
        page = folder / "reader.html"
        page.write_text("<article>正文 图1 装置示意</article>", encoding="utf-8")
        self.db.update_archive(item_id, {"title": "视觉分诊", "text": "正文" * 200 + " 图1　装置示意",
                                         "html_path": str(page), "raw_path": None, "metadata": {"images": [
                                             {"index": i, "local_path": str(blobs[i])} for i in range(3)]}})
        with patch.object(analyzer, "_llm_call", new=AsyncMock(return_value={
                "content": json.dumps({"drop": [1, 2], "keep": [3], "note": "1、2 是广告卡片"})})):
            dropped = asyncio.run(analyzer.triage_archived_images(self.db, item_id))
        # "图1" in the body is the author's first illustration, not the first <img> on
        # the page: a promo banner above it is image 1 and is never "图1".  Protecting
        # by the body numbering is what kept the banners this pass exists to remove.
        self.assertEqual(dropped, 2)
        metadata = json.loads(self.db.one("SELECT metadata_json FROM items WHERE id=?", (item_id,))["metadata_json"])
        self.assertEqual([image["index"] for image in metadata["images"]], [2])

    def test_image_triage_never_strips_every_image(self):
        from PIL import Image
        from app.services import analyzer
        item_id = self.discover("https://example.com/triage-floor", "分诊下限")
        folder = Path(self.temp.name) / "floor-doc"
        folder.mkdir()
        blobs = []
        for index in range(3):
            path = Path(self.temp.name) / f"floor-{index}.png"
            Image.new("RGB", (300, 200), (90, 90 + index * 10, 90)).save(path)
            blobs.append(path)
        page = folder / "reader.html"
        page.write_text("<article>正文</article>", encoding="utf-8")
        self.db.update_archive(item_id, {"title": "分诊下限", "text": "正文" * 200,
                                         "html_path": str(page), "raw_path": None, "metadata": {"images": [
                                             {"index": i, "local_path": str(blobs[i])} for i in range(3)]}})
        with patch.object(analyzer, "_llm_call", new=AsyncMock(return_value={
                "content": json.dumps({"drop": [1, 2, 3], "keep": [], "note": "三张都是广告"})})):
            dropped = asyncio.run(analyzer.triage_archived_images(self.db, item_id))
        self.assertEqual(dropped, 2)
        metadata = json.loads(self.db.one("SELECT metadata_json FROM items WHERE id=?", (item_id,))["metadata_json"])
        self.assertEqual(len(metadata["images"]), 1)

    def test_a_mixed_scale_answer_is_lifted_instead_of_silently_filtered(self):
        """旧模型把 relevance 当 0-10 分给，importance 当 0-100 给。

        `max(relevance, importance) <= 10` 的判断在这种情况下不成立，relevance=3
        就被当成 3/100 一路过滤掉——一篇讲轴突超微结构的科普文因此变成「低相关」。
        """
        from app.services.analyzer import _normalise_analysis
        both_low = _normalise_analysis({"relevance_score": 9, "importance_score": 6})
        self.assertEqual(both_low["relevance_score"], 90)
        self.assertEqual(both_low["importance_score"], 60)
        mixed = _normalise_analysis({"relevance_score": 3, "importance_score": 45})
        self.assertEqual(mixed["relevance_score"], 30)
        self.assertEqual(mixed["importance_score"], 45)
        # 真正的 0-100 低分不能被放大
        genuine = _normalise_analysis({"relevance_score": 12, "importance_score": 30})
        self.assertEqual(genuine["relevance_score"], 12)

    def test_picture_led_posts_are_recognised_before_the_lexical_prefilter(self):
        """信息在图里、正文只有几百字的推文不是「没抓到材料」。"""
        from app.services.analyzer import _is_picture_led
        self.assertTrue(_is_picture_led({"text": "", "metadata_json": json.dumps({"media_kind": "video"})}))
        self.assertTrue(_is_picture_led({"text": "", "metadata_json": json.dumps(
            {"archive_completeness": "images_only", "images": [{}, {}]})}))
        # 600 字配两张图：科普短文，能记录但没有论文可读
        self.assertTrue(_is_picture_led({"text": "正文" * 300, "metadata_json": json.dumps({"images": [{}, {}]})}))
        # 引了论文就该精读
        self.assertFalse(_is_picture_led({"text": "见 doi:10.1000/xyz123 的研究" * 20,
                                          "metadata_json": json.dumps({"images": [{}, {}]})}))
        # 1200 字以上是完整正文，交给云模型
        self.assertFalse(_is_picture_led({"text": "正文" * 700, "metadata_json": json.dumps({"images": [{}, {}]})}))

    def test_short_illustrated_post_is_recorded_not_flagged_as_unarchivable(self):
        from app.services import analyzer
        item_id = self.discover("https://example.com/atlas", "生殖神经支配")
        self.db.update_archive(item_id, {"title": "生殖神经支配", "text": "", "html_path": None, "raw_path": None,
                                         "metadata": {"images": [{"index": 0}, {"index": 1}],
                                                      "archive_completeness": "images_only",
                                                      "admission_filter": {"reason": "来自公众号库中已启用的重点账号"}}})
        with patch("app.services.notify.queue_for_notification", new=lambda *a, **k: None):
            analyzer._record_only(self.db, item_id, "", 2)
        row = self.db.one("SELECT status,relevance,summary,analysis_json FROM items WHERE id=?", (item_id,))
        analysis = json.loads(row["analysis_json"] or "{}")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(analysis["analysis_mode"], "record_only")
        self.assertEqual(analysis["content_shape"], "图片为主")
        self.assertIn("图文短记", row["summary"])
        # 封顶：短记不能压过真正精读过的论文，也不能触发 88 分即时推送
        self.assertLessEqual(row["relevance"], 60)
        self.assertGreaterEqual(row["relevance"], 20)

    def test_wechat_video_article_yields_a_playable_record(self):
        """视频号推文的 #js_content 是空壳，正文在 cgiData 里。"""
        from types import SimpleNamespace
        from app.services import archive
        html = ("<html><head><meta property='og:title' content='第27期 | 傅里叶变换光谱'>"
                "<meta property='og:image' content='https://mmbiz.qpic.cn/cover.jpg'></head><body>"
                "<div class='share_notice_wrp' id='js_content'></div>"
                "<script>video_page_info: { video_id: 'wxv_4782959405585580032' };"
                "files: [{ filesize: '61137278', url: 'http://mpvideo.qpic.cn/a.f10002.mp4?dis_t=1' },"
                "{ filesize: '8312017', url: 'http://mpvideo.qpic.cn/a.f10104.mp4?dis_t=1' }]</script>"
                "</body></html>")
        response = SimpleNamespace(url="https://mp.weixin.qq.com/s/vid", content=html.encode(),
                                   headers={"content-type": "text/html; charset=utf-8"}, status=200, text=html)

        async def fake_image(url, index, max_bytes):
            return {"index": index, "url": url, "sha256": "c" * 64,
                    "local_path": str(Path(self.temp.name) / "cover.png"),
                    "relative_path": "../../blobs/cc/cover.png", "bytes": 1000}

        with patch.object(archive, "request", new=AsyncMock(return_value=response)), \
                patch.object(archive, "_download_image", new=AsyncMock(side_effect=fake_image)), \
                patch.object(archive, "item_directory", return_value=Path(self.temp.name) / "video-item"):
            result = asyncio.run(archive.archive_web({"id": "v", "url": "https://mp.weixin.qq.com/s/vid",
                                                      "metadata": {}}))
        metadata = result["metadata"]
        self.assertEqual(metadata["media_kind"], "video")
        self.assertEqual(metadata["archive_completeness"], "video")
        self.assertEqual(metadata["video"]["video_id"], "wxv_4782959405585580032")
        self.assertEqual(len(metadata["video"]["sources"]), 2)
        self.assertEqual(len(metadata["images"]), 1)          # 封面留作缩略图
        reader = (Path(self.temp.name) / "video-item" / "reader.html").read_text(encoding="utf-8")
        self.assertIn("<video", reader)
        self.assertIn("f10104.mp4", reader)                  # 取最轻的一档

    def test_illustrated_page_without_prose_is_archived_not_failed(self):
        """两张解剖图、零文字的推文以前会被判成「没有提取到有效正文」。"""
        from types import SimpleNamespace
        from app.services import archive
        html = ("<html><head><meta property='og:title' content='生殖神经支配'></head><body>"
                "<div id='js_content'>"
                "<img data-src='https://mmbiz.qpic.cn/mmbiz_png/AAA/640?wx_fmt=png' data-w='1080'>"
                "<img data-src='https://mmbiz.qpic.cn/mmbiz_png/BBB/640?wx_fmt=png' data-w='1080'>"
                "</div></body></html>")
        response = SimpleNamespace(url="https://mp.weixin.qq.com/s/img", content=html.encode(),
                                   headers={"content-type": "text/html; charset=utf-8"}, status=200, text=html)

        async def fake_image(url, index, max_bytes):
            return {"index": index, "url": url, "sha256": str(index) * 64,
                    "local_path": str(Path(self.temp.name) / f"fig{index}.png"),
                    "relative_path": f"../../blobs/{index}/fig{index}.png", "bytes": 2000}

        with patch.object(archive, "request", new=AsyncMock(return_value=response)), \
                patch.object(archive, "_download_image", new=AsyncMock(side_effect=fake_image)), \
                patch.object(archive, "item_directory", return_value=Path(self.temp.name) / "img-item"):
            result = asyncio.run(archive.archive_web({"id": "i", "url": "https://mp.weixin.qq.com/s/img",
                                                      "metadata": {}}))
        self.assertEqual(result["metadata"]["archive_completeness"], "images_only")
        self.assertEqual(len(result["metadata"]["images"]), 2)
        self.assertEqual((result["text"] or "").strip(), "")


if __name__ == "__main__":
    unittest.main()
