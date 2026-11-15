"""Reversibly clean the library and repair Bilibili short links.

Default mode is a read-only dry run. Use --apply to hide non-research rows and
--resolve-links to retain original short URLs while adding stable BV URLs.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.net import USER_AGENT, validate_public_url  # noqa: E402
from app.services.relevance import assess_candidate  # noqa: E402
from app.utils import canonical_url, extract_bvid, json_loads, now_iso, stable_json  # noqa: E402


def backup_database(db: Database) -> Path:
    folder = db.path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("radar-before-research-cleanup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db")
    source = sqlite3.connect(db.path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        destination.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        destination.close()
        source.close()
    return target


async def resolve_short_link(url: str) -> tuple[str, str]:
    """Resolve redirects without downloading the video page body."""
    current = url.strip()
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=20,
                                     headers={"User-Agent": USER_AGENT}) as client:
            for _ in range(6):
                await asyncio.to_thread(validate_public_url, current)
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return "", "redirect_without_location"
                        current = urljoin(current, location)
                        continue
                    bvid = extract_bvid(str(response.url))
                    if bvid:
                        return bvid, ""
                    if response.status_code in {404, 410}:
                        return "", "gone"
                    if response.status_code >= 400:
                        return "", f"http_{response.status_code}"
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) >= 200_000:
                            break
                    body = content.decode("utf-8", errors="ignore")
                    if "json" in response.headers.get("content-type", ""):
                        try:
                            payload = json.loads(body)
                            if int(payload.get("code", 0)) in {-404, 404, -410, 410}:
                                return "", "gone"
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                    bvid = extract_bvid(body)
                    return (bvid, "") if bvid else ("", "no_bvid")
            return "", "too_many_redirects"
    except Exception as exc:
        return "", type(exc).__name__


async def repair_bilibili_links(db: Database) -> dict[str, int]:
    rows = db.query("""SELECT id,url,title,metadata_json FROM items
        WHERE kind='video' AND excluded_at IS NULL AND url LIKE '%b23.tv/%' ORDER BY discovered_at""")
    semaphore = asyncio.Semaphore(6)
    completed = 0
    counts: Counter[str] = Counter()

    async def resolve(row: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        async with semaphore:
            bvid, error = await resolve_short_link(row["url"])
        return row, bvid, error

    # Bounded batches avoid creating thousands of simultaneous connections/tasks.
    for start in range(0, len(rows), 120):
        batch = await asyncio.gather(*(resolve(row) for row in rows[start:start + 120]))
        exclusions: dict[str, list[str]] = {}
        batch_counts: Counter[str] = Counter()
        # One write transaction per network batch: fewer WAL transitions and no
        # half-written batch if Windows transiently reports a readonly database.
        with db.transaction() as conn:
            for row, bvid, error in batch:
                completed += 1
                if not bvid:
                    batch_counts["unavailable" if error == "gone" else "unresolved"] += 1
                    if error == "gone":
                        exclusions.setdefault("B站平台明确返回目标不存在（404/-404/410）", []).append(row["id"])
                    continue
                stable = canonical_url(f"https://www.bilibili.com/video/{bvid}/")
                duplicate = conn.execute("SELECT id FROM items WHERE kind='video' AND canonical_url=? AND id<>?",
                                         (stable, row["id"])).fetchone()
                if duplicate:
                    exclusions.setdefault("同一B站视频的另一条短链，已保留稳定链接条目", []).append(row["id"])
                    batch_counts["duplicates"] += 1
                    continue
                metadata = json_loads(row["metadata_json"], {}) or {}
                metadata.update({"original_share_url": row["url"], "resolved_bvid": bvid,
                                 "stable_url": stable, "link_repaired_at": now_iso()})
                title = row["title"] if row["title"] and row["title"] != row["url"] else stable
                updated = conn.execute("""UPDATE items SET url=?,canonical_url=?,title=?,metadata_json=?
                    WHERE id=? AND excluded_at IS NULL""", (stable, stable, title, stable_json(metadata), row["id"]))
                batch_counts["repaired"] += updated.rowcount
        counts.update(batch_counts)
        for reason, item_ids in exclusions.items():
            db.set_items_excluded(item_ids, reason)
        if completed % 120 == 0 or completed == len(rows):
            print(f"LINK_PROGRESS checked={completed}/{len(rows)} repaired={counts['repaired']} unresolved={counts['unresolved']}")
    return {"checked": len(rows), **counts}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="apply reversible exclusions")
    parser.add_argument("--resolve-links", action="store_true", help="resolve retained Bilibili short links")
    args = parser.parse_args()

    db = Database()
    rows = db.query("SELECT * FROM items WHERE excluded_at IS NULL")
    groups: dict[str, list[str]] = {}
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        assessment = assess_candidate(row, include_archived_text=True)
        counts[(row["kind"], "keep" if assessment["keep"] else assessment["reason"])] += 1
        if not assessment["keep"]:
            groups.setdefault(assessment["reason"], []).append(row["id"])
    for key, count in counts.most_common():
        print("DRY_RUN", key, count)
    print("DRY_RUN_TOTAL", {"visible": len(rows), "keep": len(rows) - sum(map(len, groups.values())),
                             "exclude": sum(map(len, groups.values()))})
    if not args.apply and not args.resolve_links:
        return

    backup = backup_database(db)
    print("BACKUP", backup)
    if args.apply:
        changed = 0
        for reason, item_ids in groups.items():
            changed += db.set_items_excluded(item_ids, reason)
        print("EXCLUDED", changed)
    if args.resolve_links:
        result = asyncio.run(repair_bilibili_links(db))
        print("LINK_REPAIR", json.dumps(result, ensure_ascii=False))
        db.add_event("links_repaired", "B站短链补全：" + json.dumps(result, ensure_ascii=False))
    print("VISIBLE_AFTER", db.stats())


if __name__ == "__main__":
    main()
