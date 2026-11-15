"""Retry failed article archives by fetching the public page directly.

No paid fallback is involved: every item is fetched from its own public URL with
a normal browser identity, exactly like opening the link in a browser.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Database, row_with_json
from app.services.archive import archive_item


async def retry_one(db: Database, row: dict, semaphore: asyncio.Semaphore, stats: dict) -> None:
    async with semaphore:
        item = row_with_json(row, "metadata_json", "tags_json", "analysis_json")
        try:
            result = await archive_item(item)
        except Exception as exc:
            stats["failed"] += 1
            print("FAILED", (row.get("title") or "")[:50], type(exc).__name__, str(exc)[:140], flush=True)
            return
        db.update_archive(row["id"], result)
        stats["ok"] += 1
        print("OK", (row.get("title") or "")[:50], len(result.get("text") or ""), flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description="按原文链接重试失败的公众号归档")
    parser.add_argument("--limit", type=int, default=300, help="最多重试多少条")
    parser.add_argument("--concurrency", type=int, default=3, help="并发数，默认 3")
    parser.add_argument("--apply", action="store_true", help="真正写入；缺省只统计")
    args = parser.parse_args()

    db = Database()
    rows = db.query("""SELECT * FROM items WHERE status='error' AND excluded_at IS NULL
        ORDER BY discovered_at LIMIT ?""", (max(1, args.limit),))
    print({"mode": "apply" if args.apply else "dry_run", "candidates": len(rows)}, flush=True)
    if not args.apply or not rows:
        return 0

    stats = {"ok": 0, "failed": 0}
    semaphore = asyncio.Semaphore(max(1, min(6, args.concurrency)))
    await asyncio.gather(*(retry_one(db, row, semaphore, stats) for row in rows))
    remaining = db.one("SELECT COUNT(*) c FROM items WHERE status='error' AND excluded_at IS NULL")["c"]
    print({"done": True, **stats, "remaining_errors": remaining})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
