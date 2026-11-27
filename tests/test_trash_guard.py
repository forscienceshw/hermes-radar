"""`tools/trash.py wipe` 的守卫。

2026-11-18 现场：把 `wipe E:/HermesResearchRadar/app` 当成"清回收站里该前缀的
条目"用，结果删掉的是生产目录的 app/ 本身，api 与 worker 每 4 秒崩一次。
规定：wipe 只删你亲手给的路径，且受 git 跟踪的路径一律拒绝；清回收站条目是 purge。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def _load_trash():
    spec = importlib.util.spec_from_file_location("radar_trash_guard", ROOT / "tools" / "trash.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TrashGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trash = _load_trash()

    def test_tracked_path_is_recognized(self) -> None:
        self.assertTrue(self.trash._git_tracked(str(ROOT / "tools" / "trash.py")))
        self.assertTrue(self.trash._git_tracked(str(ROOT / "app")))

    def test_outside_repo_is_not_tracked(self) -> None:
        with TemporaryDirectory() as temp:
            self.assertFalse(self.trash._git_tracked(temp))

    def test_wipe_refuses_tracked_path(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "keepme.txt"
            target.write_text("x", encoding="utf-8")
            args = argparse.Namespace(paths=[str(target)], keep_trash=True, force=False)
            original = self.trash._git_tracked
            self.trash._git_tracked = lambda _path: True
            try:
                self.trash.cmd_wipe(args)
            finally:
                self.trash._git_tracked = original
            self.assertTrue(target.exists(), "受 git 跟踪的路径不该被删掉")

    def test_force_still_deletes(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "gone.txt"
            target.write_text("x", encoding="utf-8")
            args = argparse.Namespace(paths=[str(target)], keep_trash=True, force=True)
            original = self.trash._git_tracked
            self.trash._git_tracked = lambda _path: True
            try:
                self.trash.cmd_wipe(args)
            finally:
                self.trash._git_tracked = original
            self.assertFalse(target.exists(), "--force 就是明确要删")

    def test_leftover_trash_counts_live_project_entries(self) -> None:
        """只看本项目前缀、且有实体数据的条目；别的盘/用户自己的东西不算。"""
        fake = [
            {"original": str(ROOT / "data" / "documents" / "a" / "raw.html"),
             "size": 100, "has_data": True},
            {"original": str(ROOT / "data" / "blobs" / "b.pdf"),
             "size": 50, "has_data": False},
            {"original": "D:\\照片\\x.jpg", "size": 999, "has_data": True},
        ]
        got = self.trash.leftover_trash([str(ROOT)], fake)
        self.assertEqual(got["count"], 1)
        self.assertEqual(got["size"], 100)

    def test_project_prefixes_follows_data_override(self) -> None:
        original = os.environ.get("RADAR_DATA_DIR")
        os.environ["RADAR_DATA_DIR"] = "F:\\radar-data"
        try:
            prefixes = self.trash.project_prefixes()
        finally:
            if original is None:
                os.environ.pop("RADAR_DATA_DIR", None)
            else:
                os.environ["RADAR_DATA_DIR"] = original
        self.assertTrue(any(p.casefold().endswith("radar-data") for p in prefixes),
                        "换了 RADAR_DATA_DIR 之后，资料库前缀也要跟着换")


if __name__ == "__main__":
    unittest.main()
