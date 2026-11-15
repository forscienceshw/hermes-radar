"""删除没有任何条目引用的图片/PDF blob。

内容寻址存储不会覆盖旧文件：重归档、失败任务和早期 40 张图上限留下了一批
没有任何 metadata 引用的文件。脚本先按数据库里的全部引用建立白名单，再删除
其余文件；默认只统计，加 --apply 才真正删除。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.paths import BLOB_DIR, DB_PATH  # noqa: E402

BLOB_REF_RE = re.compile(r"blobs[\\/]([0-9a-f]{2})[\\/]([0-9a-f]{16,64}\.[a-z0-9]{1,12})")


def referenced_keys(db: Database) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in db.query("SELECT metadata_json,raw_path,html_path FROM items"):
        for text in (row.get("metadata_json"), row.get("raw_path"), row.get("html_path")):
            if not text:
                continue
            keys.update((m.group(1), m.group(2)) for m in BLOB_REF_RE.finditer(str(text)))
    for row in db.query("SELECT local_path,text_path FROM papers"):
        for text in (row.get("local_path"), row.get("text_path")):
            if not text:
                continue
            keys.update((m.group(1), m.group(2)) for m in BLOB_REF_RE.finditer(str(text)))
    return keys


def prune(apply: bool = False) -> dict[str, int]:
    db = Database(DB_PATH)
    keys = referenced_keys(db)
    files = [path for path in BLOB_DIR.rglob("*") if path.is_file()]
    orphans = [path for path in files if (path.parent.name, path.name) not in keys]
    freed = sum(path.stat().st_size for path in orphans)
    if apply:
        for path in orphans:
            try:
                path.unlink()
            except OSError:
                pass
    return {"files": len(files), "referenced": len(keys), "orphans": len(orphans),
            "orphan_mb": round(freed / 1024 / 1024, 1), "deleted": len(orphans) if apply else 0}


def main() -> None:
    parser = argparse.ArgumentParser(description="清理没有条目引用的 blob 文件")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只统计）")
    args = parser.parse_args()
    print(prune(args.apply))


if __name__ == "__main__":
    main()
