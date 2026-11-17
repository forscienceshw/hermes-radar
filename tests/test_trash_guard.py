"""`tools/trash.py wipe` 的守卫。

2026-11-18 现场：把 `wipe E:/HermesResearchRadar/app` 当成"清回收站里该前缀的
条目"用，结果删掉的是生产目录的 app/ 本身，api 与 worker 每 4 秒崩一次。
规定：wipe 只删你亲手给的路径，且受 git 跟踪的路径一律拒绝；清回收站条目是 purge。
"""
from __future__ import annotations

import argparse
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
