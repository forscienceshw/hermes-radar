"""测试包的公共前置。

这台机器给 python 挂了"删除即入回收站"的垫片（`sitecustomize` 把 `os.remove` /
`shutil.rmtree` / `Path.unlink` 换成了移入回收站）。测试每跑一遍，`TemporaryDirectory`
里的临时库和 SQLite 的 `-shm` 就在回收站里堆出两三百条，删得比不删还慢。

**根本解法在 `app/native_delete.py`**：导入 app 包时把垫片换掉的删除函数装回原始
版本，整个进程（包括服务）从此真删除。这里再补一层：`TemporaryDirectory` 的清理
换成 `app/services/purge.py`（一次 `SHFileOperationW`，不带 `FOF_ALLOWUNDO`），
比逐个 unlink 快。
"""
from __future__ import annotations

import atexit
import gc
import tempfile
import time
from pathlib import Path

try:
    from app.services import purge
except Exception:                                    # noqa: BLE001 - 没装依赖时照常跑测试
    purge = None

# 根本解法在 app/native_delete.py：导入 app 包时就把垫片换掉的删除函数装回原始版本，
# 所以测试进程里的 `os.remove` / `shutil.rmtree` 已经是真删除了。这里只补一层：
# `TemporaryDirectory` 的清理换成一次 `SHFileOperationW`，比逐个 unlink 快。
from app.native_delete import restore_native_deletes as _restore_native_deletes  # noqa: E402

_RESTORED = _restore_native_deletes()


if purge is not None and hasattr(tempfile.TemporaryDirectory, "_rmtree"):
    _original = tempfile.TemporaryDirectory._rmtree

    def _rmtree(name, ignore_errors: bool = False):   # noqa: ANN001 - 对齐标准库签名
        if purge.tree(name):
            return
        # 极少数情况（文件被占用）退回去用标准库的，至少别把测试晾在这儿
        _original(name, ignore_errors=ignore_errors)

    tempfile.TemporaryDirectory._rmtree = staticmethod(_rmtree)


# 上面那层只管得住 TemporaryDirectory。SQLite 自己在关闭连接时删 `-shm` /
# `-journal` 走的是 CPython 的 `os.unlink`，垫片照样接管——一趟全量测试能留下
# 四百多条。测试跑完统一扫一次，只认本项目前缀，别的软件一条不碰。
_ROOT = Path(__file__).resolve().parent.parent


def _sweep_trash(rounds: int = 3) -> None:
    """先把还没关闭的连接收掉，再扫。

    sqlite3 的 `-shm` / `-journal` 是在连接对象被回收时才删的，而解释器退出时的
    回收发生在 atexit 之后——不先 `gc.collect()` 一次，扫的是还没生成的条目。
    剩下几条第下次进来再扫，净增量为零。
    """
    try:
        from app.services import backup
    except Exception:                                # noqa: BLE001
        return
    for index in range(max(1, rounds)):
        try:
            gc.collect()
        except Exception:                            # noqa: BLE001
            pass
        try:
            backup.sweep_trash(prefixes=[str(_ROOT)])
        except Exception:                            # noqa: BLE001 - 清扫失败不许影响测试结果
            pass
        if index + 1 < rounds:
            time.sleep(0.2)


if purge is not None:
    # 上一次跑剩下的一两条，这次进来先扫掉，免得越攒越多
    _sweep_trash(rounds=1)
    atexit.register(_sweep_trash)
