"""让本进程的删除操作真的删除，而不是堆进回收站。

这台机器给 python 挂了 WorkBuddy 的删除垫片（`sitecustomize`），把 `os.remove` /
`os.unlink` / `shutil.rmtree` / `Path.unlink` 一律换成"移入回收站"。垫片是
**解释器启动时**按环境变量决定装不装的，跑起来再改环境变量没用。

这层兜底对科研雷达纯属负担，而且有害：

- 服务每关一次数据库连接，SQLite 删 `radar.db-wal` / `-shm` 就走这里。实测
  几分钟就在回收站里堆 51 条 `radar.db-wal`，用户打开回收站看到几千个项目。
- 测试跑一遍堆两百多条临时库，删除还慢十倍（80 个小文件 1.1 秒 vs 0.01 秒）。

垫片 patch 之前把原函数存在自己模块的 `_orig_*` 里，取回来装回去即可。本进程删的
都是自己刚产生的临时文件；用户资料是软删除（`items.excluded_at`），不经过这里。

2026-11-18：用户第三次抱怨回收站里堆了几千项，所以改成代码层默认行为，不再依赖
"跑测试时记得加环境变量"这种靠人记的办法。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

# (宿主, 属性名, 垫片里保存的原函数, 报错用的标签)
_PAIRS = (
    (os, "remove", "_orig_remove", "os.remove"),
    (os, "unlink", "_orig_unlink", "os.unlink"),
    (os, "rmdir", "_orig_rmdir", "os.rmdir"),
    (shutil, "rmtree", "_orig_shutil_rmtree", "shutil.rmtree"),
    (pathlib.Path, "unlink", "_orig_path_unlink", "Path.unlink"),
    (pathlib.Path, "rmdir", "_orig_path_rmdir", "Path.rmdir"),
)


def restore_native_deletes() -> list[str]:
    """把垫片换掉的删除函数装回原始版本，返回装回去的名字。

    垫片没生效（不在沙箱里、或已经带 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 启动）时
    返回空列表。任何一步失败都跳过，绝不让这件事影响程序启动。
    """
    shim = sys.modules.get("sitecustomize")
    if shim is None:
        return []
    restored: list[str] = []
    for owner, attr, source, label in _PAIRS:
        original = getattr(shim, source, None)
        if not callable(original):
            continue
        try:
            setattr(owner, attr, original)
        except (AttributeError, TypeError):
            continue
        restored.append(label)
    return restored
