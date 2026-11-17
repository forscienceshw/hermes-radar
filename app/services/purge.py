"""直删：删掉就是删掉，不进回收站。

为什么不用 `shutil.rmtree` / `os.remove`：这台机器（以及任何装了同类"安全删除"
垫片的环境）会在 Python 启动时把这些 API 换成"移入回收站"。开发机上一天就能在
回收站里堆出上千条 `test.db-shm`，用户看到的是"删个东西卡半天"和"回收站里全是
垃圾"。而清理构建产物、临时目录、自己下错的安装包，本来就不该留副本。

实现的是一次 `SHFileOperationW`，**不带** `FOF_ALLOWUNDO`，所以是彻头彻尾的删除，
也不经过 Python 那一层。非 Windows 上退回普通删除。

注意：这个模块只给"我们自己造出来的东西"用 —— 临时文件、缓存、旧安装包、
滚动备份。用户资料在别处，别拿它碰 `data/documents`、`data/blobs`。
"""
from __future__ import annotations

import ctypes
import os
import shutil
from pathlib import Path

FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200

if os.name == "nt":
    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    _FO_DELETE = 3


def _win_delete(paths: list[Path]) -> bool:
    """一次调用删一批；路径必须是绝对路径，且以 \\0\\0 结尾。"""
    if not paths:
        return True
    # 双空字符结尾是 SHFileOperation 的硬性要求，少一个就会静默失败
    joined = "\0".join(str(path) for path in paths) + "\0\0"
    struct = _SHFILEOPSTRUCTW()
    struct.hwnd = None
    struct.wFunc = _FO_DELETE
    struct.pFrom = joined
    struct.pTo = None
    struct.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(struct))
    return result == 0 and not struct.fAnyOperationsAborted


def remove(*paths: str | Path) -> dict[str, list[str]]:
    """永久删除文件或目录。返回还剩下来的路径，方便调用方判断成败。"""
    targets = [Path(path).expanduser().absolute() for path in paths]
    targets = [path for path in targets if path.exists() or path.is_symlink()]
    if not targets:
        return {"failed": []}

    if os.name == "nt":
        # 软链接本身要删掉，不要跟进去把目标盘的内容端了
        _win_delete(targets)
        left = [str(path) for path in targets if path.exists() or path.is_symlink()]
        if not left:
            return {"failed": []}
        # 被占用、权限不足之类的漏网之鱼再逐个试一次
        targets = [Path(item) for item in left]
    failed: list[str] = []
    for path in targets:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=False)
            else:
                path.unlink()
        except OSError:
            failed.append(str(path))
    return {"failed": failed}


def tree(path: str | Path) -> bool:
    """删一棵树，成功返回 True。"""
    return not remove(path)["failed"]


def unlink(path: str | Path) -> bool:
    """删一个文件（或软链接），成功返回 True。"""
    return tree(path)
