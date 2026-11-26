"""子进程窗口策略：图形宿主里永远不要给用户弹控制台。

软件窗口跑在 ``pythonw.exe`` 或 PyInstaller ``--windowed`` 的 exe 里，本身没有
控制台。这时启动一个控制台子系统的小工具（``tasklist``、``pip``、``python -c``、
``node``），Windows 会**为子进程新建一个控制台窗口**——用户看到黑框一闪，像是
程序出了什么事。所有非交互的子进程都必须显式关窗，别指望子进程自己礼貌。

规矩（写进 FEATURES 与 skill，别在新代码里破例）：

* 后台跑、只要结果的子进程 → ``hidden_kwargs()`` / ``spawn()``，窗口一个都不许有；
* 真的要给人输入或看实时输出（装依赖、跑测试）→ 放在命令行入口里执行，由用户
  在终端里自己看，不要从图形界面拉起来。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Win32 进程创建标志。用字面量而不是 getattr(subprocess, ...)，非 Windows 也读得到。
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def hidden_kwargs(extra_flags: int = 0) -> dict[str, Any]:
    """``subprocess.Popen/run`` 的"别弹窗"参数；非 Windows 返回空字典。

    ``extra_flags`` 会和 ``CREATE_NO_WINDOW`` 合并，用来附加 ``DETACHED_PROCESS``
    之类的标志——脱离父进程但依然不许有窗口。
    """
    if os.name != "nt":
        return {}
    flags = CREATE_NO_WINDOW | extra_flags
    return {"creationflags": flags}


def spawn(args: Sequence[str | Path], **kwargs: Any) -> subprocess.Popen:
    """起一个纯后台子进程：无窗口、三个标准流默认丢弃。

    需要读输出的（例如 ``tasklist``）自己传 ``stdout=subprocess.PIPE``。
    """
    options = hidden_kwargs(int(kwargs.pop("extra_flags", 0)))
    for stream in ("stdin", "stdout", "stderr"):
        options.setdefault(stream, subprocess.DEVNULL)
    options.update(kwargs)
    if "creationflags" in kwargs:                    # 调用方自己指定了就合并
        options["creationflags"] = kwargs["creationflags"] | CREATE_NO_WINDOW
    return subprocess.Popen([str(item) for item in args], **options)


def run_hidden(args: Sequence[str | Path], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` 的关窗版本，其余参数原样透传。"""
    options = hidden_kwargs(int(kwargs.pop("extra_flags", 0)))
    options.update(kwargs)
    if "creationflags" in kwargs:
        options["creationflags"] = kwargs["creationflags"] | CREATE_NO_WINDOW
    return subprocess.run([str(item) for item in args], **options)
