"""资料库的每日快照。

为什么雷达自己要留一份：`data/` 是几个月攒下来的正文、配图、精读结果和论文。
一次误删、一次 `git clean`、一次装错目录，就没有第二份了。SQLite 的在线 backup
API 不锁库，采集和精读照跑，所以这个成本很低。

保留最近 7 份（`BACKUP_KEEP`），滚动删除**自己产生的**旧快照。用户自己的
`data/documents`、`data/blobs` 不在这里，也不会被这段代码碰到。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..paths import DATA_DIR, DB_PATH
from . import purge

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 7
BACKUP_PREFIX = "radar-"


def _stamp() -> str:
    return time.strftime("%Y%m%d")


def backups() -> list[Path]:
    if not BACKUP_DIR.is_dir():
        return []
    return sorted(BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.db"))


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
    try:
        with sqlite3.connect(str(DB_PATH)) as source, sqlite3.connect(str(temp)) as dest:
            source.backup(dest)
        temp.replace(target)
    except Exception as exc:                            # noqa: BLE001 - 备份失败不该拖垮主流程
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "created": ""}

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
    return {
        "dir": str(BACKUP_DIR),
        "count": len(items),
        "latest": str(latest) if latest else "",
        "latest_size": latest.stat().st_size if latest else 0,
        "latest_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(latest.stat().st_mtime)) if latest else "",
    }
