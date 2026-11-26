"""子进程不许弹控制台窗口。

用户看到的症状：软件好好跑着，偶尔一个黑框闪一下就没了。根因不是崩溃，是图形
宿主（pythonw / --windowed）自己没有控制台，拉起控制台程序时 Windows 会给子进程
新建一个窗口。装一次软件能看到七八个，运行时借用客户端登录态也能看到一个。

这组测试锁两件事：

1. 仓库里所有会被图形宿主执行的子进程调用，都必须带 ``creationflags``/``startupinfo``；
   以后谁写漏了，``test_audited_paths_are_clean`` 直接红。
2. ``app/winproc.py`` 的封装真的会加上 ``CREATE_NO_WINDOW``，``client_pids()`` 在
   psutil 可用时完全不起子进程。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import winproc
from app.services import workbuddy_direct
from tools import console_audit

ROOT = Path(__file__).resolve().parents[1]


class RepositoryAuditTest(unittest.TestCase):
    """图形宿主路径下，一处都不许漏。"""

    def test_audited_paths_are_clean(self):
        spawns = console_audit.audit(ROOT)
        bad = [(str(item.path), item.line, item.kind) for item in spawns if not item.allowed]
        self.assertEqual([], bad, "这些子进程没关窗，用户会看到黑框一闪")
        self.assertGreater(len(spawns), 10, "审计范围像是配错了，认出的调用太少")


class ScannerSelfTest(unittest.TestCase):
    """审计脚本自己不能失灵——漏报比不报更糟。"""

    def _scan(self, source: str) -> list[console_audit.Spawn]:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "probe.py"
            path.write_text(source, encoding="utf-8")
            return console_audit.scan_file(path, Path(folder))

    def test_flags_bare_call(self):
        found = self._scan("import subprocess\nsubprocess.run(['tasklist'])\n")
        self.assertEqual(1, len(found))
        self.assertFalse(found[0].allowed)

    def test_accepts_creationflags_and_marker(self):
        found = self._scan(
            "import subprocess\n"
            "subprocess.run(['a'], creationflags=0x08000000)\n"
            "subprocess.run(['b'])  # console-ok: 要给人看输出\n"
            "subprocess.run(['c'], **{'creationflags': 1})\n"
        )
        self.assertEqual(3, len(found))
        self.assertTrue(all(item.allowed for item in found))

    def test_flags_os_system_and_asyncio(self):
        found = self._scan(
            "import os, asyncio\n"
            "os.system('dir')\n"
            "asyncio.create_subprocess_exec('node')\n"
        )
        self.assertEqual({"os.system", "asyncio.create_subprocess_exec"},
                         {item.kind for item in found})
        self.assertTrue(all(not item.allowed for item in found))


class HiddenSpawnTest(unittest.TestCase):
    """winproc 的封装必须真的把标志传下去。"""

    def test_hidden_kwargs_sets_no_window_on_windows(self):
        if sys.platform != "win32":
            self.assertEqual({}, winproc.hidden_kwargs())
            return
        flags = winproc.hidden_kwargs()["creationflags"]
        self.assertTrue(flags & winproc.CREATE_NO_WINDOW)
        merged = winproc.hidden_kwargs(winproc.DETACHED_PROCESS)["creationflags"]
        self.assertTrue(merged & winproc.CREATE_NO_WINDOW)
        self.assertTrue(merged & winproc.DETACHED_PROCESS)

    def test_spawn_redirects_streams(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            winproc.spawn(["tool.exe"])
        _, kwargs = popen.call_args
        self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
        self.assertEqual(subprocess.DEVNULL, kwargs["stdout"])
        self.assertEqual(subprocess.DEVNULL, kwargs["stderr"])
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & winproc.CREATE_NO_WINDOW)

    def test_spawn_merges_caller_flags(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            winproc.spawn(["tool.exe"], creationflags=winproc.DETACHED_PROCESS)
        _, kwargs = popen.call_args
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & winproc.DETACHED_PROCESS)
            self.assertTrue(kwargs["creationflags"] & winproc.CREATE_NO_WINDOW)


class ClientPidsTest(unittest.TestCase):
    """借客户端登录态是运行时高频路径，不能再起 tasklist。"""

    def test_client_pids_does_not_spawn_when_psutil_present(self):
        try:
            import psutil  # noqa: F401
        except ImportError:
            self.skipTest("这台机器没有 psutil")
        with mock.patch.object(workbuddy_direct.subprocess, "run") as run:
            workbuddy_direct.client_pids()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
