"""磁盘策略的契约测试。

这几条一旦被改掉，表现都是"磁盘悄悄又涨回去了"，不会报错、不会崩，所以必须锁住：

1. 原文快照默认压缩落盘，且读回来和原文一字不差（无损，不是有损压缩）。
2. 关掉开关时退回未压缩的 raw.html，老数据（未压缩）仍然读得到。
3. 配图上限 0 表示不限——以前写成 `[:0]` 会一张图都不存。
4. 设置页的默认值与接口暴露的数据目录必须真实，不能是开发机的 E 盘。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.services import archive
from app.settings import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]


class RawSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.payload = ('<html><body><p>科研雷达原文快照</p>'
                        '<img src="data:image/png;base64,' + "A" * 5000 + '"></body></html>').encode()

    def tearDown(self) -> None:
        try:
            self.temp.cleanup()
        except BaseException:            # noqa: BLE001 - 垫片可能拒绝 rmtree
            pass

    def test_default_is_gzip_and_lossless(self) -> None:
        with mock.patch.object(archive, "get_settings", return_value={"storage": {"compress_raw": True}}):
            path = archive.write_raw(self.folder, self.payload)
        self.assertEqual(path.name, "raw.html.gz", "默认应该直接写 gz")
        self.assertFalse((self.folder / "raw.html").exists(), "压缩后不该再留原文件")
        self.assertEqual(archive.read_raw_bytes(path), self.payload, "gzip 必须无损")
        # 老记录只存了 raw.html 这个路径，读取侧要能自己找到 .gz
        self.assertEqual(archive.raw_file(self.folder / "raw.html"), path)

    def test_switch_off_writes_plain_html(self) -> None:
        with mock.patch.object(archive, "get_settings", return_value={"storage": {"compress_raw": False}}):
            path = archive.write_raw(self.folder, self.payload)
        self.assertEqual(path.name, "raw.html")
        self.assertEqual(archive.read_raw_text(path), self.payload.decode())

    def test_rearchive_does_not_leave_stale_plain_copy(self) -> None:
        """同一篇重新归档时，旧的 raw.html 必须清掉，否则读取会优先拿到旧内容。"""
        (self.folder / "raw.html").write_bytes(b"<html>old</html>")
        with mock.patch.object(archive, "get_settings", return_value={"storage": {"compress_raw": True}}):
            path = archive.write_raw(self.folder, self.payload)
        self.assertFalse((self.folder / "raw.html").exists())
        self.assertEqual(archive.read_raw_bytes(path), self.payload)


class StorageDefaultsTests(unittest.TestCase):
    def test_defaults_are_space_aware(self) -> None:
        storage = DEFAULTS.get("storage") or {}
        self.assertTrue(storage.get("compress_raw"), "新条目默认要压缩原文快照")
        self.assertLessEqual(int(storage.get("backup_keep") or 7), 3, "每日快照默认别留太多份")
        self.assertLessEqual(int(DEFAULTS["max_images_per_article"]), 8, "配图上限别回到 12 张")

    def test_example_settings_matches_defaults(self) -> None:
        import json
        example = json.loads((ROOT / "config" / "settings.example.json").read_text(encoding="utf-8"))
        self.assertTrue((example.get("storage") or {}).get("compress_raw"))
        self.assertIn("max_images_per_article", example)


class StorageUsageTests(unittest.TestCase):
    """设置页的占用统计要按顶层目录聚合，根目录的库文件单独算一格。"""

    def test_usage_groups_by_top_level_dir(self) -> None:
        from app import api

        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "documents").mkdir()
            (root / "documents" / "a.html").write_bytes(b"x" * 100)
            (root / "blobs").mkdir()
            (root / "blobs" / "b.png").write_bytes(b"y" * 300)
            (root / "radar.db").write_bytes(b"z" * 10)
            with mock.patch.object(api, "DATA_DIR", root), \
                    mock.patch.object(api, "_STORAGE_CACHE", {"at": 0.0, "data": None}):
                data = api.storage_usage(refresh=1)
        self.assertEqual([p["name"] for p in data["parts"]][:2], ["blobs", "documents"])
        self.assertIn("（根目录文件）", [p["name"] for p in data["parts"]])
        self.assertEqual(data["total_files"], 3)
        self.assertEqual(data["total_bytes"], 410)


class FrontendContractTests(unittest.TestCase):
    """界面不能写死某台机器的路径，也不能再自动弹智能收藏。"""

    def setUp(self) -> None:
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    def test_storage_path_comes_from_api(self) -> None:
        self.assertIn('id="storagePath"', self.html)
        self.assertIn("applyStoragePath", self.js)
        self.assertIn("h.data_dir", self.js)
        self.assertNotIn("E:\\HermesResearchRadar\\data", self.html, "左下角不许写死数据路径")

    def test_smart_collect_is_manual(self) -> None:
        self.assertIn("data-smart-collect", self.js, "顶栏要有智能收藏按钮")
        self.assertNotIn("setTimeout(()=>suggestCollections", self.js,
                         "打开条目不该再自动弹智能收藏")
        self.assertIn("suggestCollections(x,{manual:true})", self.js)

    def test_storage_settings_controls_exist(self) -> None:
        for control in ("maxImages", "compressRaw", "backupKeep", "storageUsageInfo"):
            self.assertIn(f'id="{control}"', self.html)
        self.assertIn("max_images_per_article:Number($('#maxImages').value)", self.js)


if __name__ == "__main__":
    unittest.main()
