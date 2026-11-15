"""Rebuild archived reader pages with the current zoomable template.

The reader is a static HTML file per item, so a template change only reaches new
archives.  This walks the existing ones, keeps the already-sanitised article body,
and re-renders the shell so every page gets the image viewer.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.paths import DB_PATH  # noqa: E402
from app.services.archive import _reader_html  # noqa: E402
from app.storage import atomic_write  # noqa: E402

ARTICLE_RE = re.compile(r"<article>(.*?)</article>", re.S)
NOTICE_RE = re.compile(r'<div class="notice">(.*?)</div>', re.S)


def upgrade(limit: int = 0, dry_run: bool = False) -> dict[str, int]:
    db = Database(DB_PATH)
    rows = db.query("""SELECT id,title,author,url,html_path FROM items
        WHERE html_path IS NOT NULL AND html_path<>'' ORDER BY discovered_at DESC""")
    stats = {"scanned": 0, "upgraded": 0, "skipped": 0, "missing": 0}
    for row in rows:
        path = Path(row["html_path"])
        if not path.is_file():
            stats["missing"] += 1
            continue
        stats["scanned"] += 1
        content = path.read_text(encoding="utf-8", errors="replace")
        # Both markers must be present: the viewer alone means an older shell that
        # predates the per-figure size labels.
        if 'id="imageViewer"' in content and "imgdim" in content:
            stats["skipped"] += 1
            continue
        match = ARTICLE_RE.search(content)
        if not match:
            stats["skipped"] += 1
            continue
        notice = NOTICE_RE.search(content)
        warning = re.sub(r"<[^>]+>", "", notice.group(1)).strip() if notice else ""
        rendered = _reader_html(row["title"] or "未命名", row["author"] or "", row["url"] or "",
                                match.group(1), warning)
        if not dry_run:
            atomic_write(path, rendered)
        stats["upgraded"] += 1
        if limit and stats["upgraded"] >= limit:
            break
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="为已有离线阅读页补上图片缩放查看器")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少页（0 表示全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = parser.parse_args()
    print(upgrade(args.limit, args.dry_run))


if __name__ == "__main__":
    main()
