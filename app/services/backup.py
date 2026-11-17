"""资料库的每日快照。

为什么雷达自己要留一份：`data/` 是几个月攒下来的正文、配图、精读结果和论文。
一次误删、一次 `git clean`、一次装错目录，就没有第二份了。SQLite 的在线 backup
API 不锁库，采集和精读照跑，所以这个成本很低。

保留最近 7 份（`BACKUP_KEEP`），滚动删除**自己产生的**旧快照。用户自己的
`data/documents`、`data/blobs` 不在这里，也不会被这段代码碰到。
"""
from __future__ import annotations

import importlib.util
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..paths import BASE_DIR, DATA_DIR, DB_PATH
from . import purge

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 7
BACKUP_PREFIX = "radar-"
# 只认"每日快照"这一种命名。`radar-before-*` 是出事前手动留的底，
# 属于"重要文件"，永远不进滚动删除的范围。
DAILY = re.compile(r"^radar-\d{8}\.db$")


def _stamp() -> str:
    return time.strftime("%Y%m%d")


def backups() -> list[Path]:
    """每日快照，按时间从旧到新。"""
    if not BACKUP_DIR.is_dir():
        return []
    items = [path for path in BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.db") if DAILY.match(path.name)]
    return sorted(items, key=lambda path: path.stat().st_mtime)


def run(force: bool = False) -> dict[str, Any]:
    """做一份今天的快照；今天已经有了就跳过（`force` 除外）。"""
    if not DB_PATH.is_file():
        return {"ok": False, "error": "还没有数据库文件", "created": ""}
    target = BACKUP_DIR / f"{BACKUP_PREFIX}{_stamp()}.db"
    if target.exists() and not force:
        return {"ok": True, "created": "", "skipped": True, "target": str(target),
                "count": len(backups())}

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".db.part")
    if temp.exists():                                   # 上次失败留下的半成品
        purge.unlink(temp)
    source = dest = None
    try:
        # 一定要显式 close：`with sqlite3.connect(...)` 只提交事务、不关连接，
        # 连接还开着的时候 Windows 不允许 rename，快照会永远停在 .part。
        source = sqlite3.connect(str(DB_PATH))
        dest = sqlite3.connect(str(temp))
        source.backup(dest)
    except Exception as exc:                            # noqa: BLE001 - 备份失败不该拖垮主流程
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "created": ""}
    finally:
        if dest is not None:
            dest.close()
        if source is not None:
            source.close()
    try:
        temp.replace(target)
    except OSError as exc:
        purge.unlink(temp)
        return {"ok": False, "error": f"重命名失败：{exc}", "created": ""}

    removed = 0
    for old in backups()[:-BACKUP_KEEP]:
        try:
            purge.unlink(old)
            removed += 1
        except OSError:
            continue
    return {"ok": True, "created": str(target), "removed": removed,
            "size": target.stat().st_size, "count": len(backups())}


def status() -> dict[str, Any]:
    items = backups()
    latest = items[-1] if items else None
    manual = len([p for p in BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.db") if not DAILY.match(p.name)]) \
        if BACKUP_DIR.is_dir() else 0
    return {
        "dir": str(BACKUP_DIR),
        "count": len(items),
        "manual": manual,
        "latest": str(latest) if latest else "",
        "latest_size": latest.stat().st_size if latest else 0,
        "latest_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(latest.stat().st_mtime)) if latest else "",
    }


def _trash_module():
    """把 `tools/trash.py` 当模块装上。它本来就是个独立脚本，这里只是复用。"""
    path = BASE_DIR / "tools" / "trash.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("radar_trash", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sweep_trash() -> int:
    """把回收站里**本项目目录下**的记录彻底删掉，返回清掉的条数。

    为什么会有这些记录：应用删临时文件时，有一部分删除是 CPython / sqlite3 内部
    调 `os.unlink` 做的（比如关闭连接时的 `radar.db-wal`），本机的"安全删除"垫片
    会把它们塞进回收站，一天几十条。删的是我们自己刚造出来的文件，回收站留副本
    没有任何意义。

    只按 `BASE_DIR` / `DATA_DIR` 前缀匹配——别的软件、用户自己的文件一条都不碰。
    """
    module = _trash_module()
    if module is None:
        return 0
    prefixes = [str(BASE_DIR), str(DATA_DIR)]
    try:
        items = [item for item in module.entries()
                 if module._matches(item["original"], prefixes)]
    except Exception:                                   # noqa: BLE001 - 清扫失败不影响备份
        return 0
    removed = 0
    for item in items:
        try:
            if item["has_data"]:
                module._delete_path(str(item["data"]))
            module._delete_path(str(item["meta"]))
            removed += 1
        except Exception:                               # noqa: BLE001
            continue
    return removed
