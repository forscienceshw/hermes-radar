"""自动更新这一层的回归测试。

这里不碰网络：httpx 的请求全部被替身接管，安装包下载走本地临时文件。
真正要守住的三件事 ——
  1. 版本号比较不能把 1.10.0 判成比 1.9.0 旧（字符串比大小就会踩这个坑）；
  2. 任何一个源挂掉都不能让 check() 抛异常，断网时软件必须照常跑；
  3. 装完之后 state.json 里还留着上一版的 latest，status() 必须按当前版本重新算，
     否则界面会一直显示"有新版本"，用户点进去又装不了。
"""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import updater


class VersionTests(unittest.TestCase):
    def test_tuple_handles_common_shapes(self):
        self.assertEqual(updater.version_tuple("1.2.3"), (1, 2, 3))
        self.assertEqual(updater.version_tuple("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater.version_tuple("1.2"), (1, 2, 0))
        self.assertEqual(updater.version_tuple("1.2.3-beta.4"), (1, 2, 3))
        self.assertEqual(updater.version_tuple(""), (0, 0, 0))

    def test_ten_is_newer_than_nine(self):
        # 字符串比较会把 "1.10.0" 判成小于 "1.9.0"，这是最经典的翻车点。
        self.assertTrue(updater.is_newer("1.10.0", "1.9.0"))
        self.assertFalse(updater.is_newer("1.9.0", "1.10.0"))

    def test_same_version_is_not_newer(self):
        self.assertFalse(updater.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(updater.is_newer("v1.0.0", "1.0.0"))


class ParseTests(unittest.TestCase):
    def test_version_json_shape(self):
        payload = {
            "version": "1.2.0",
            "notes": "加了自动更新",
            "released_at": "2026-11-17T20:00:00+08:00",
            "asset": {"name": "RadarSetup.exe", "url": "https://example.com/a.exe",
                      "size": 1234, "sha256": "ABCDEF"},
        }
        parsed = updater._from_version_json(payload)
        self.assertEqual(parsed["latest"], "1.2.0")
        self.assertEqual(parsed["sha256"], "abcdef")   # 统一转小写，校验时才不会误判
        self.assertEqual(parsed["size"], 1234)

    def test_release_json_picks_our_asset(self):
        payload = {
            "tag_name": "v1.3.0",
            "body": "修好了封面",
            "assets": [
                {"name": "source.zip", "size": 10, "browser_download_url": "https://example.com/s.zip"},
                {"name": "RadarSetup.exe", "size": 99, "browser_download_url": "https://example.com/r.exe"},
            ],
        }
        parsed = updater._from_release_json(payload)
        self.assertEqual(parsed["latest"], "1.3.0")
        self.assertEqual(parsed["url"], "https://example.com/r.exe")
        self.assertEqual(parsed["size"], 99)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            mock.patch.object(updater, "STATE_PATH", self.root / "state.json"),
            mock.patch.object(updater, "UPDATE_DIR", self.root),
            mock.patch.object(updater, "VERSION_URLS", ("https://a.invalid/version.json",)),
            mock.patch.object(updater, "API_LATEST", "https://b.invalid/latest"),
            mock.patch.object(updater, "current_version", lambda: "1.0.0"),
        ]
        for item in self.patches:
            item.start()
        updater._progress.update({"state": "idle", "percent": 0})

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_every_source_down_is_not_an_exception(self):
        with mock.patch.object(updater, "_client", side_effect=OSError("断网")):
            result = updater.check(timeout=1)
        self.assertFalse(result["ok"])
        self.assertFalse(result["has_update"])
        # 错误信息里保留"哪个源、什么错"，但不带异常正文（可能是几百行 HTML）
        self.assertIn("a.invalid", result["error"])
        self.assertIn("OSError", result["error"])

    def test_finds_newer_version_from_version_json(self):
        payload = {"version": "1.1.0", "notes": "新功能",
                   "asset": {"url": "https://example.com/r.exe", "size": 100, "sha256": "aa"}}

        class FakeResponse:
            status_code = 200

            def json(self):
                return payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url):
                return FakeResponse()

        with mock.patch.object(updater, "_client", lambda timeout: FakeClient()):
            result = updater.check(timeout=1)
        self.assertTrue(result["ok"])
        self.assertTrue(result["has_update"])
        self.assertEqual(result["latest"], "1.1.0")

    def test_status_recomputes_after_install(self):
        # 装完之后 VERSION 变成 1.1.0，但 state.json 里的 latest 还是 1.1.0
        (self.root / "state.json").write_text(json.dumps({
            "latest": "1.1.0", "notes": "新版", "checked_at": "2026-11-17 20:00:00",
            "has_update": True, "size": 100,
        }), encoding="utf-8")
        with mock.patch.object(updater, "current_version", lambda: "1.1.0"):
            state = updater.status()
        self.assertFalse(state["has_update"], "装完之后不该再提示有新版本")
        self.assertEqual(state["current"], "1.1.0")

    def test_status_still_flags_update_when_behind(self):
        (self.root / "state.json").write_text(json.dumps({
            "latest": "1.2.0", "notes": "新版", "has_update": True,
        }), encoding="utf-8")
        state = updater.status()
        self.assertTrue(state["has_update"])
        self.assertFalse(state["downloaded"])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            mock.patch.object(updater, "UPDATE_DIR", self.root),
            mock.patch.object(updater, "STATE_PATH", self.root / "state.json"),
        ]
        for item in self.patches:
            item.start()
        updater._progress.update({"state": "idle", "percent": 0})

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def _fake_stream(self, payload: bytes, total: int | None = None):
        class FakeResponse:
            status_code = 200
            headers = {"content-length": str(total if total is not None else len(payload))}

            def iter_bytes(self, size):
                for index in range(0, len(payload), max(1, size)):
                    yield payload[index:index + size]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def stream(self, method, url):
                return FakeResponse()

        return FakeClient()

    def test_download_verifies_sha256(self):
        payload = b"MZ" + b"\0" * (2 * 1024 * 1024)   # 大于 1 MB，_downloaded_path 才认
        digest = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(updater, "get_settings", lambda: {}), \
                mock.patch("httpx.Client", lambda **kwargs: self._fake_stream(payload)):
            path = updater.download("https://example.com/r.exe", "1.1.0", digest)
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_size, len(payload))
        self.assertEqual(updater.download_state()["state"], "done")

    def test_bad_checksum_is_rejected(self):
        payload = b"MZ" + b"\0" * (2 * 1024 * 1024)
        with mock.patch.object(updater, "get_settings", lambda: {}), \
                mock.patch("httpx.Client", lambda **kwargs: self._fake_stream(payload)):
            with self.assertRaises(RuntimeError):
                updater.download("https://example.com/r.exe", "1.1.0", "0" * 64)
        self.assertEqual(updater.download_state()["state"], "error")
        self.assertFalse(list(self.root.glob("*.part")), "校验失败要留下干净的目录")


class MirrorTests(unittest.TestCase):
    def test_mirror_prefix_only_touches_github(self):
        with mock.patch.object(updater, "get_settings", lambda: {"update_mirror": "https://ghproxy.net/"}):
            self.assertEqual(updater._mirrored(
                "https://github.com/o/r/releases/download/v1/a.exe"),
                "https://ghproxy.net/o/r/releases/download/v1/a.exe")
            self.assertEqual(updater._mirrored("https://example.com/a.exe"),
                             "https://example.com/a.exe")

    def test_no_mirror_keeps_url(self):
        with mock.patch.object(updater, "get_settings", lambda: {}):
            url = "https://github.com/o/r/releases/download/v1/a.exe"
            self.assertEqual(updater._mirrored(url), url)


if __name__ == "__main__":
    unittest.main()
