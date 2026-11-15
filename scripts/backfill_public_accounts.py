"""Replay a bounded history window of official-account cards from local WeChat.

The incremental reader only follows rows newer than its cursor, so anything
published before the source was enabled never reaches the radar. This script
re-reads a time window for enabled catalog accounts and never touches the
incremental cursor, so live collection keeps working unchanged.
"""
from __future__ import annotations

import argparse
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Database
from app.services import wechat_self_archive as archive
from app.services.relevance import assess_candidate
from app.utils import epoch_iso, now_iso


def name_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def pick_accounts(directory: dict[str, dict[str, Any]], keywords: list[str]) -> set[str]:
    wanted = [name_key(word) for word in keywords if word.strip()]
    if not wanted:
        return set()
    picked: set[str] = set()
    for user, info in directory.items():
        aliases = set(info.get("aliases") or set())
        aliases.add(name_key(info.get("name") or ""))
        if any(any(word in alias for alias in aliases) for word in wanted):
            picked.add(user)
    return picked


def catalog_accounts(config: dict[str, Any], directory: dict[str, dict[str, Any]]) -> set[str]:
    allowed, _, _ = archive._public_account_filter(config, directory)
    return {user for user in allowed if user in directory}


def fetch_rows(conn, talkers: list[str], since: int, limit: int) -> list[tuple]:
    rows: list[tuple] = []
    for start in range(0, len(talkers), 400):
        chunk = talkers[start:start + 400]
        marks = ",".join("?" * len(chunk))
        rows.extend(conn.execute(
            f"""SELECT localId,CreateTime,StrTalker,StrContent,CompressContent FROM PublicMsg
                WHERE Type=49 AND SubType=5 AND CreateTime>=? AND StrTalker IN ({marks})
                ORDER BY localId""", [since, *chunk]).fetchall())
        if len(rows) >= limit:
            break
    rows.sort(key=lambda row: row[0])
    return rows[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="回放本机微信公众号历史文章（不改增量游标）")
    parser.add_argument("--account", action="append", default=[], help="公众号名称关键词，可重复")
    parser.add_argument("--days", type=int, default=30, help="回放最近多少天，默认 30")
    parser.add_argument("--limit", type=int, default=2000, help="最多读取多少条卡片")
    parser.add_argument("--apply", action="store_true", help="写入数据库；缺省为只读预演")
    parser.add_argument("--process", action="store_true", help="入库后立即进入归档/处理队列")
    parser.add_argument("--all-catalog", action="store_true", help="回放公众号库中全部启用账号")
    args = parser.parse_args()

    db = Database()
    source = db.one("SELECT * FROM sources WHERE type='wechat_public_local'")
    if not source:
        raise SystemExit("尚未配置本机公众号来源")
    config = archive.json_loads(source.get("config_json"), {}) or {}

    _, root, key = archive._account_and_key()
    directory = archive._official_account_directory(root, key)
    names = {user: info["name"] for user, info in directory.items()}

    if args.account:
        talkers = pick_accounts(directory, args.account)
    elif args.all_catalog:
        talkers = catalog_accounts(config, directory)
    else:
        raise SystemExit("请用 --account 指定公众号，或加 --all-catalog 回放全部启用账号")
    if not talkers:
        print({"matched_accounts": 0, "hint": "没有匹配的公众号，请检查名称"})
        return 0
    catalog_set = catalog_accounts(config, directory)

    days = max(1, min(365, args.days))
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    conn = archive._open_live_readonly(root / "Msg" / "PublicMsg.db", key)
    try:
        rows = fetch_rows(conn, sorted(talkers), since, max(20, args.limit))
    finally:
        conn.close()

    stats = {"matched_accounts": len(talkers), "scanned": len(rows), "parsed": 0,
             "filtered": 0, "created": 0, "duplicate": 0, "unparsed": 0, "errors": 0}
    samples: list[dict[str, Any]] = []
    for local_id, created, talker, text, compressed in rows:
        title, description, url, _ = archive._card(compressed, str(text or ""))
        host = (urlsplit(url).hostname or "").lower() if url else ""
        if host != "mp.weixin.qq.com" and not host.endswith(".mp.weixin.qq.com"):
            stats["unparsed"] += 1
            continue
        try:
            canonical = archive.canonical_url(url)
        except ValueError:
            stats["unparsed"] += 1
            continue
        stats["parsed"] += 1
        candidate = {
            "source_id": source["id"], "kind": "article", "url": url, "canonical_url": canonical,
            "external_key": f"public:{local_id}", "title": title or url,
            "author": names.get(talker, talker), "published_at": epoch_iso(created),
            "metadata": {"share_channel": "wechat_public_local_backfill", "wechat_account_id": talker,
                         "public_message_id": local_id, "description": description,
                         "catalog_matched": talker in catalog_set,
                         "backfilled_at": now_iso(), "backfill_window_days": days},
            "defer_processing": not args.process,
        }
        assessment = assess_candidate(candidate)
        if not assessment["keep"]:
            stats["filtered"] += 1
            continue
        candidate["metadata"]["admission_filter"] = {"reason": assessment["reason"],
                                                     "matched_terms": assessment["matched_terms"]}
        if not args.process:
            candidate["metadata"]["processing_policy"] = "history_index_only"
        if len(samples) < 15:
            samples.append({"published_at": candidate["published_at"], "author": candidate["author"],
                            "title": (title or "")[:90], "kept": True})
        if not args.apply:
            continue
        try:
            _, created_new = db.discover_item(candidate)
        except Exception as exc:  # keep going; report a count instead of aborting the batch
            stats["errors"] += 1
            print("ITEM_ERROR", type(exc).__name__, str(exc)[:160])
            continue
        stats["created" if created_new else "duplicate"] += 1

    print({"mode": "apply" if args.apply else "dry_run", "days": days, **stats,
           "samples": samples})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
