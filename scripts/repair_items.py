"""Repair two historical data problems that both looked like "the filter is wrong".

1. The old `hy4free` model answered on a 0-10 scale (3 meant "fairly relevant").
   Every downstream comparison uses a 0-100 threshold, so nine good posts were
   filed as 低相关 with a score of 3.

2. `insufficient` used to require 1200 characters of body text.  A complete
   700-character popular-science post is not truncated material; calling it
   「无法详细归档」 was wrong.  Those now go through the short-post rule instead.

    python scripts/repair_items.py --dry-run
    python scripts/repair_items.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Database                      # noqa: E402
from app.settings import get_settings            # noqa: E402
from app.utils import json_loads, now_iso, stable_json  # noqa: E402


def fix_scales(db: Database, apply: bool) -> dict[str, int]:
    """Lift answers the model gave on a 0-10 scale to the 0-100 one we compare on."""
    settings = get_settings()
    threshold = float(settings["notifications"]["min_relevance"])
    rows = db.query("""SELECT id,title,relevance,importance,status,analysis_json FROM items
        WHERE analyzed_at IS NOT NULL AND relevance>0 AND relevance<=10""")
    stats = {"scanned": len(rows), "fixed": 0}
    for row in rows:
        analysis = json_loads(row["analysis_json"], {}) or {}
        relevance = float(analysis.get("relevance_score") or row["relevance"] or 0)
        importance = float(analysis.get("importance_score") or 0)
        if not 0 < max(relevance, importance) <= 10:
            continue
        lifted = round(relevance * 10, 1)
        # Never launder an archive failure into `ready`: only a previously filtered
        # item can be promoted by the corrected score.
        status = row["status"] if row["status"] != "filtered" else ("ready" if lifted >= threshold else "filtered")
        print(f"  {row['id'][:8]}  {relevance:5.1f} -> {lifted:5.1f}  {row['status']:9s} -> {status:9s}  "
              f"{(row['title'] or '')[:38]}")
        if not apply:
            continue
        analysis["relevance_score"] = lifted
        analysis["importance_score"] = round(importance * 10, 1)
        analysis["scale_repaired"] = {"from": [relevance, importance], "at": now_iso()}
        db.execute("""UPDATE items SET relevance=?,importance=?,analysis_json=?,status=?,
                      category=COALESCE(category,?) WHERE id=?""",
                   (lifted, analysis["importance_score"], stable_json(analysis), status,
                    analysis.get("category") or "综合科研", row["id"]))
        stats["fixed"] += 1
    return stats


async def rerun_short(db: Database, apply: bool, limit: int) -> dict[str, int]:
    """Re-file `insufficient` items as short records instead of unarchivable ones.

    These were never failures: they are 500-1200 character posts whose information
    sits in the text and figures.  The short-post rule records them without calling
    the cloud model at all, which is both cheaper and more honest than a reading
    that has nothing to read.
    """
    from app.services.analyzer import _record_only

    rows = db.query("""SELECT id,title,text,metadata_json FROM items WHERE status='insufficient'
                       AND excluded_at IS NULL ORDER BY discovered_at DESC LIMIT ?""", (limit,))
    stats = {"scanned": len(rows), "recorded": 0}
    for row in rows:
        body = (row["text"] or "").strip()
        metadata = json_loads(row["metadata_json"], {}) or {}
        images = len(metadata.get("images") or [])
        print(f"  {row['id'][:8]}  正文 {len(body):5d} 字  图 {images:2d} 张  {(row['title'] or '')[:40]}")
        if not apply:
            continue
        try:
            _record_only(db, row["id"], body, images)
            fresh = db.one("SELECT status,analysis_json FROM items WHERE id=?", (row["id"],))
            mode = (json_loads(fresh["analysis_json"], {}) or {}).get("analysis_mode") if fresh else "?"
            print(f"           -> {fresh['status']} / {mode}")
            stats["recorded"] += 1
        except Exception as exc:                       # noqa: BLE001 - report and continue
            print(f"           !! {type(exc).__name__}: {exc}")
    return stats


def requeue_failed(db: Database, apply: bool, only: list[str]) -> dict[str, int]:
    """Put items whose archive raised a hard error back into the archive queue."""
    sql = "SELECT id,title,error FROM items WHERE status='error' AND excluded_at IS NULL"
    params: tuple = ()
    if only:
        sql += " AND (" + " OR ".join("id LIKE ?" for _ in only) + ")"
        params = tuple(prefix + "%" for prefix in only)
    rows = db.query(sql, params)
    stats = {"scanned": len(rows), "queued": 0}
    for row in rows:
        print(f"  {row['id'][:8]}  {(row['title'] or '')[:46]}")
        print(f"           {str(row['error'] or '')[:96]}")
        if not apply:
            continue
        db.execute("UPDATE items SET status='discovered',error=NULL WHERE id=?", (row["id"],))
        queued = db.enqueue_job("archive_item", {"version": 1, "item_id": row["id"]},
                                dedupe_key=f"archive:{row['id']}:repair-{now_iso()}",
                                priority=40, max_attempts=4)
        stats["queued"] += 1 if queued else 0
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="写入改动；缺省只预览")
    parser.add_argument("--scales", action="store_true", help="修 0-10 刻度的历史评分")
    parser.add_argument("--short", action="store_true", help="重跑 status=insufficient 的条目")
    parser.add_argument("--archive", action="store_true", help="把 status=error 的条目重新入队归档")
    parser.add_argument("--only", action="append", default=[], help="限定条目 id 前缀（可多次）")
    parser.add_argument("--limit", type=int, default=60, help="--short 最多处理多少条")
    args = parser.parse_args()
    if not (args.scales or args.short or args.archive):
        parser.error("至少指定 --scales / --short / --archive 之一")

    db = Database("data/radar.db")
    result: dict[str, object] = {"apply": args.apply}
    if args.scales:
        print("== 修正 0-10 刻度的历史评分 ==")
        result["scales"] = fix_scales(db, args.apply)
    if args.short:
        print("== 重跑 insufficient（图文短记） ==")
        result["short"] = asyncio.run(rerun_short(db, args.apply, args.limit))
    if args.archive:
        print("== 重新入队归档失败的条目 ==")
        result["archive"] = requeue_failed(db, args.apply, args.only)
    print()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
