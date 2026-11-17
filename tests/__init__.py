"""测试包的公共前置。

这台机器给 python 挂了"删除即入回收站"的垫片（`sitecustomize` 把 `os.remove` /
`shutil.rmtree` / `Path.unlink` 换成了移入回收站）。测试每跑一遍，`TemporaryDirectory`
里的临时库和 SQLite 的 `-shm` 就会在回收站里堆出两三百条，删得比不删还慢，用户看到
的是"回收站里全是垃圾"。

临时目录里全是我们自己刚造出来的东西，直接抹掉就对了。所以在导入测试包时把
`TemporaryDirectory` 的清理换成 `app/services/purge.py`（一次 `SHFileOperationW`，
不带 `FOF_ALLOWUNDO`），从源头断掉。
"""
from __future__ import annotations

import tempfile

try:
    from app.services import purge
except Exception:                                    # noqa: BLE001 - 没装依赖时照常跑测试
    purge = None


if purge is not None and hasattr(tempfile.TemporaryDirectory, "_rmtree"):
    _original = tempfile.TemporaryDirectory._rmtree

    def _rmtree(name, ignore_errors: bool = False):   # noqa: ANN001 - 对齐标准库签名
        if purge.tree(name):
            return
        # 极少数情况（文件被占用）退回去用标准库的，至少别把测试晾在这儿
        _original(name, ignore_errors=ignore_errors)

    tempfile.TemporaryDirectory._rmtree = staticmethod(_rmtree)
