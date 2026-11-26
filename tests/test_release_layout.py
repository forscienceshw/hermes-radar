"""发布包目录的契约：安装包不许和源码、软件安装目录混在一起。

这条规则是用户明确提的，而且很容易在下次改打包脚本时被顺手改回 `dist/`，
所以用测试钉住，而不是写在注释里。
"""
import os
import unittest
from pathlib import Path
from unittest import mock

from tools import release_paths


class ReleaseLayoutTests(unittest.TestCase):
    def test_release_dir_is_outside_the_project(self):
        target = release_paths.release_dir()
        root = release_paths.ROOT
        self.assertNotEqual(target, root)
        self.assertNotIn(root, target.parents, "发布目录不能在仓库里面")

    def test_each_version_gets_its_own_folder(self):
        first = release_paths.version_dir("1.1.0")
        second = release_paths.version_dir("1.2.0")
        self.assertNotEqual(first, second)
        self.assertEqual(first.name, "v1.1.0")
        self.assertEqual(first.parent, release_paths.release_dir())

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"RADAR_RELEASE_DIR": r"D:\Somewhere\Releases"}):
            self.assertEqual(release_paths.release_dir(), Path(r"D:\Somewhere\Releases").resolve())

    def test_build_intermediate_is_outside_too(self):
        build = release_paths.build_dir()
        self.assertIn(release_paths.release_dir(), build.parents)
        self.assertNotIn(release_paths.ROOT, build.parents)


if __name__ == "__main__":
    unittest.main()
