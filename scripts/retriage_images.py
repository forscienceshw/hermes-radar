"""Re-judge archived images for articles that never went through triage.

Triage used to run only while reading an article, so everything archived before
that change still carries its ad banners, QR cards and group photos.  One vision
call per article is enough; this script walks the backlog newest-and-largest
first and stops when the daily LLM budget is close.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.paths import DB_PATH  # noqa: E402
from app.services.analyzer import triage_archived_images  # noqa: E402
from app.settings import get_settings  # noqa: E402
from app.utils import json_loads, local_date  # noqa: E402


def backlog(db: Database) -> list[tuple[str, str, int]]:
    """Items with at least three archived images and no stored verdict yet."""
    rows = db.query("""SELECT id,title,metadata_json FROM items
        WHERE status='ready' AND excluded_at IS NULL AND metadata_json LIKE '%local_path%'
        ORDER BY discovered_at DESC""")
    pending: list[tuple[str, str, int]] = []
    for row in rows:
        metadata = json_loads(row.get("metadata_json"), {}) or {}
        if metadata.get("image_triage"):
            continue
        images = [image for image in (metadata.get("images") or []) if image.get("local_path")]
        if len(images) < 3:
            continue
        pending.append((row["id"], row.get("title") or "", len(images)))
    pending.sort(key=lambda entry: entry[2], reverse=True)
    return pending


def calls_left(db: Database) -> int:
    limit = int(get_settings()["llm"].get("daily_call_limit") or 0)
    row = db.one("SELECT value FROM runtime WHERE key=?", ("llm_calls:" + local_date(),))
    used = int(row["value"]) if row else 0
    return max(0, limit - used) if limit else 10**6


async def run(limit: int, dry_run: bool, reserve: int) -> dict[str, int]:
    db = Database(DB_PATH)
    pending = backlog(db)
    budget = max(0, calls_left(db) - reserve)
    stats = {"pending": len(pending), "budget": budget, "processed": 0,
             "dropped": 0, "skipped": 0, "failed": 0}
    if dry_run:
        for item_id, title, count in pending[:limit]:
            print(f"{item_id[:8]}  {count:2d} 张  {title[:40]}")
        return stats
    for item_id, title, count in pending[:limit]:
        if stats["processed"] >= budget:
            break
        try:
            dropped = await triage_archived_images(db, item_id, force=True)
        except Exception as exc:  # noqa: BLE001 - one bad article must not stop the sweep
            stats["failed"] += 1
            print(f"FAIL {item_id[:8]} {type(exc).__name__}: {exc}")
            continue
        stats["processed"] += 1
        stats["dropped"] += int(dropped)
        print(f"OK   {item_id[:8]}  {count:2d} 张 -> 删 {dropped}  | {title[:36]}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="批量补跑已归档文章的配图分诊")
    parser.add_argument("--limit", type=int, default=40, help="本次最多处理多少篇")
    parser.add_argument("--reserve", type=int, default=30, help="给当日其它调用留出的余量")
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理条目，不调用模型")
    args = parser.parse_args()
    stats = asyncio.run(run(args.limit, args.dry_run, args.reserve))
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
