"""只读体检：一次群发多篇里，被归并吞掉了多少篇。

背景：微信一条群发挂几篇（3.x 是并排几个 ``<appmsg>``，4.x 靠 ``idx`` 区分）。
2026-11-19 之前三处读取器都只取第一篇，靠下的那几篇连候选都不是。修好之后只能
保证"以后不再漏"，历史里已经漏掉的还得单独补，所以先量一下有多大。

判据只认一种情况：**这一行确实被收录过**（至少一篇在资料库），其它同批篇目
却不在。整行都没收录的说明本来就没被准入规则放行，不算归并的锅。

只读：不写库、不建任务、不动微信文件。

用法::

    python tools/audit_multi.py            # 近 90 天
    python tools/audit_multi.py --days 7   # 近 7 天
    python tools/audit_multi.py --json     # 给程序读
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.paths import DATA_DIR
from app.services import wechat3_public
from app.utils import canonical_url


def audit(days: int) -> dict:
    snap = wechat3_public.snapshot()
    if not snap:
        return {"available": False, "reason": "没有可用的 3.x PublicMsg.db，或密钥失效"}

    since = int(time.time()) - max(1, days) * 86400
    con = sqlite3.connect("file:" + str(snap["db"]) + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT localId,CreateTime,StrTalker,StrContent,CompressContent,DisplayContent "
            "FROM PublicMsg WHERE Type=49 AND SubType=5 AND CreateTime>=? ORDER BY localId",
            (since,)).fetchall()
    finally:
        con.close()

    lib = sqlite3.connect("file:" + str(DATA_DIR / "radar.db") + "?mode=ro", uri=True)
    lib.row_factory = sqlite3.Row
    try:
        known = {row["canonical_url"] for row in lib.execute("SELECT canonical_url FROM items")}
        names, enabled = {}, set()
        for row in lib.execute("SELECT account_id,name,enabled FROM wechat_accounts WHERE account_id<>''"):
            names[row["account_id"]] = row["name"]
            if int(row["enabled"] or 0):
                enabled.add(row["account_id"])
    finally:
        lib.close()

    partly = 0
    missing: list[dict] = []
    for row in rows:
        if row["StrTalker"] not in enabled:
            continue
        cards = wechat3_public.cards(row["CompressContent"],
                                    row["StrContent"] or row["DisplayContent"] or "")
        if len(cards) < 2:
            continue
        hits = []
        for card in cards:
            url = card.get("url") or ""
            if "mp.weixin.qq.com" not in url:
                continue
            try:
                hits.append((card, canonical_url(url)))
            except ValueError:
                continue
        if not any(canon in known for _, canon in hits):
            continue                    # 整行都没收录，跟归并无关
        partly += 1
        for card, canon in hits:
            if canon in known:
                continue
            missing.append({"at": row["CreateTime"], "local_id": row["localId"],
                            "idx": int(card.get("idx") or 1),
                            "account": names.get(row["StrTalker"], row["StrTalker"]),
                            "account_id": row["StrTalker"],
                            "title": card.get("title", ""), "url": card["url"]})

    per_account: dict[str, int] = {}
    for item in missing:
        per_account[item["account"]] = per_account.get(item["account"], 0) + 1
    return {"available": True, "days": days, "rows": len(rows),
            "partly_collected_rows": partly, "missing": len(missing),
            "per_account": dict(sorted(per_account.items(), key=lambda kv: -kv[1])),
            "items": sorted(missing, key=lambda x: -x["at"])}


def main() -> int:
    parser = argparse.ArgumentParser(description="统计一次群发多篇里被吞掉的篇目（只读）")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=15, help="打印最近多少条")
    args = parser.parse_args()

    result = audit(args.days)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not result.get("available"):
        print(result.get("reason"))
        return 1
    print(f"窗口 {result['days']} 天：卡片 {result['rows']} 行，部分收录的多篇群发 "
          f"{result['partly_collected_rows']} 行，被吞掉 {result['missing']} 条")
    for name, count in list(result["per_account"].items())[:12]:
        print(f"  {name}: {count}")
    for item in result["items"][:max(0, args.limit)]:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["at"]))
        print(f"  {stamp} idx={item['idx']} [{item['account']}] {item['title'][:46]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
