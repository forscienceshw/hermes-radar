"""拿本机微信的真实关注列表，校准公众号库（wechat_accounts）里的名字和 gh 编号。

背景：`wechat_accounts` 这 1460 条是 2026-11-13 从手机长截图 OCR 一次性导入的，
`account_id` 几乎全是 NULL，名字里带一堆 OCR 错字（`Al-Protein Design` 其实是
`AI-Protein Design`）。运行时的 gh 号全靠运行时按名字去 Contact 表里猜，猜不中就
整个号静默失联。

这个脚本把权威来源（微信 contact.db 里 `username LIKE 'gh_%'` 的 1717 个号）
拉出来，和库里的名字做三档匹配，只读体检 -> 人工过一遍 -> 写回。

用法：
    python tools/calibrate_accounts.py             # 只读体检，打印报告
    python tools/calibrate_accounts.py --apply     # 把确认唯一的写回库里
    python tools/calibrate_accounts.py --json out.json
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database                                    # noqa: E402
from app.services import wechat4_live as W                      # noqa: E402
from app.utils import name_match_key, now_iso                   # noqa: E402
from app.services.wechat_self_archive import _account_name_key  # noqa: E402


def real_directory() -> dict[str, str]:
    """gh_xxx -> 备注名/昵称。优先用已解密缓存，没有再现场解密。"""
    names = W.account_names(W.DEFAULT_CACHE)
    gh = {k: v for k, v in names.items() if str(k).startswith("gh_") and str(v or "").strip()}
    if not gh:
        W.refresh()
        names = W.account_names(W.DEFAULT_CACHE)
        gh = {k: v for k, v in names.items() if str(k).startswith("gh_") and str(v or "").strip()}
    return gh


def build_index(real: dict[str, str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    exact: dict[str, list[str]] = defaultdict(list)
    folded: dict[str, list[str]] = defaultdict(list)
    for user, name in real.items():
        exact[_account_name_key(name)].append(user)
        folded[name_match_key(name)].append(user)
    return exact, folded


def match(name: str, exact, folded, real) -> tuple[str, list[str]]:
    """返回 (档位, 命中的 gh 列表)。档位：exact / folded / loose / none。"""
    key = _account_name_key(name)
    hits = exact.get(key) or []
    if len(hits) == 1:
        return "exact", hits
    if len(hits) > 1:
        return "ambiguous", hits
    hits = folded.get(name_match_key(name)) or []
    if len(hits) == 1:
        return "folded", hits
    if len(hits) > 1:
        return "ambiguous", hits
    if len(key) >= 4:
        loose = [u for u, n in real.items()
                 if len(_account_name_key(n)) >= 4
                 and (key in _account_name_key(n) or _account_name_key(n) in key)]
        if len(loose) == 1:
            return "loose", loose
        if len(loose) > 1:
            return "ambiguous", loose[:6]
    return "none", []


# 汉字形近字、漏字、OCR 截断（`漆草` 实为 `潦草`、`数字李生` 实为 `数字孪生`）
# 光靠折叠救不回来，只能按字面相似度猜。猜错会把 A 号的文章当 B 号收进来，
# 所以要求「分数够高 + 领先第二名够远」两个条件同时成立。
FUZZY_MIN = 0.80
FUZZY_GAP = 0.15


def fuzzy_match(name: str, real: dict[str, str]) -> tuple[str, list[str], float, float]:
    """返回 (档位, 候选, 最高分, 与第二名的差距)。"""
    probe = str(name or "").rstrip(".．… ").strip()
    if len(probe) < 3:
        return "none", [], 0.0, 0.0
    scored = sorted(((difflib.SequenceMatcher(None, probe, n).ratio(), u)
                     for u, n in real.items()), reverse=True)[:2]
    if not scored:
        return "none", [], 0.0, 0.0
    best_score, best_user = scored[0]
    gap = best_score - (scored[1][0] if len(scored) > 1 else 0.0)
    if best_score >= FUZZY_MIN and gap >= FUZZY_GAP:
        return "fuzzy", [best_user], best_score, gap
    return "weak", [best_user], best_score, gap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="把 exact/folded 唯一命中写回库")
    ap.add_argument("--with-fuzzy", action="store_true", help="连形近字（高分手动阈值）一起写回")
    ap.add_argument("--with-loose", action="store_true", help="连唯一包含命中的（简称/带后缀）一起写回，只动启用的")
    ap.add_argument("--json", default="", help="报告另存为 JSON")
    args = ap.parse_args()

    real = real_directory()
    if not real:
        print("读不到微信关注列表（contact.db 解密失败或微信没登录）")
        return 2
    exact, folded = build_index(real)

    rows = Database().query("SELECT id,name,account_id,enabled,fit FROM wechat_accounts ORDER BY enabled DESC,name")
    buckets: dict[str, list[dict]] = defaultdict(list)
    report = []
    for row in rows:
        if str(row.get("account_id") or "").startswith("gh_"):
            grade, hits, score, gap = "already", [row["account_id"]], 1.0, 1.0
        else:
            grade, hits = match(str(row["name"] or ""), exact, folded, real)
            if grade == "none":
                grade, hits, score, gap = fuzzy_match(str(row["name"] or ""), real)
            else:
                score, gap = 1.0, 1.0
        item = {"id": row["id"], "name": row["name"], "enabled": int(row["enabled"] or 0),
                "grade": grade, "gh": hits, "score": round(score, 3), "gap": round(gap, 3),
                "real_name": [real[h] for h in hits if h in real][:6]}
        report.append(item)
        buckets[grade].append(item)

    # 只有精确/折叠/已回填这三档算「名单已覆盖」；猜出来的不算，
    # 否则缺号统计会被自己猜中的候选掩掉。
    used = {h for item in report if item["grade"] in ("already", "exact", "folded") for h in item["gh"]}
    missing = {u: n for u, n in real.items() if u not in used}

    print(f"数据库真实关注 {len(real)} 个号；库内名单 {len(rows)} 条")
    for grade in ("already", "exact", "folded", "fuzzy", "loose", "ambiguous", "weak", "none"):
        items = buckets.get(grade, [])
        on = sum(1 for i in items if i["enabled"])
        print(f"  {grade:10s} {len(items):5d} 条（其中启用 {on}）")
    print(f"  数据库里有、名单里完全没有的号：{len(missing)}")

    if buckets.get("folded"):
        print("\n-- 折叠能救回来的（OCR 错字，折叠后一致）--")
        for i in buckets["folded"]:
            print(f"  {'[启用]' if i['enabled'] else '[停用]'} {i['name']}  ->  {i['real_name']} ({i['gh'][0]})")
    if buckets.get("fuzzy"):
        print("\n-- 字面最像、且明显领先第二名（形近字/漏字/截断，可自动改）--")
        for i in buckets["fuzzy"]:
            print(f"  {'[启用]' if i['enabled'] else '[停用]'} {i['name']}  ->  {i['real_name'][0]} "
                  f"({i['gh'][0]}, {i['score']:.2f}/差{i['gap']:.2f})")
    if buckets.get("weak"):
        print("\n-- 有候选但不够像，必须人工确认 --")
        for i in buckets["weak"]:
            print(f"  {'[启用]' if i['enabled'] else '[停用]'} {i['name']}  ~  {i['real_name'][0]} "
                  f"({i['gh'][0]}, {i['score']:.2f}/差{i['gap']:.2f})")
    if buckets.get("loose"):
        print("\n-- 靠包含关系猜到的（必须人工确认）--")
        for i in buckets["loose"]:
            print(f"  {'[启用]' if i['enabled'] else '[停用]'} {i['name']}  ~  {i['real_name'][0]} ({i['gh'][0]})")
    if buckets.get("none"):
        print("\n-- 真实名单里完全没有相似的（OCR 乱码，或这个号已不在关注列表）--")
        for i in buckets["none"]:
            print(f"  {'[启用]' if i['enabled'] else '[停用]'} {i['name']}")
    if buckets.get("ambiguous"):
        print("\n-- 撞车（多个号同名，不能自动改）--")
        for i in buckets["ambiguous"]:
            print(f"  {'[启用]' if i['enabled'] else '[停用]'} {i['name']}  ->  {i['real_name']}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"real_total": len(real), "rows": report,
             "missing_from_catalog": missing}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n报告已写入 {args.json}")

    if args.apply:
        db = Database()
        grades = ["exact", "folded"] + (["fuzzy"] if args.with_fuzzy else [])
        changed = []
        stamp = now_iso()
        for grade in grades:
            for i in buckets.get(grade, []):
                gh = i["gh"][0]
                new_name = real.get(gh) or i["name"]
                db.execute("UPDATE wechat_accounts SET account_id=?,name=?,updated_at=? WHERE id=?",
                           (gh, new_name, stamp, i["id"]))
                changed.append({"id": i["id"], "old": i["name"], "new": new_name, "gh": gh, "grade": grade})
        if args.with_loose:
            for i in buckets.get("loose", []):
                if not i["enabled"]:
                    continue                      # 停用的号改了也只是噪音
                gh = i["gh"][0]
                new_name = real.get(gh) or i["name"]
                db.execute("UPDATE wechat_accounts SET account_id=?,name=?,updated_at=? WHERE id=?",
                           (gh, new_name, stamp, i["id"]))
                changed.append({"id": i["id"], "old": i["name"], "new": new_name, "gh": gh, "grade": "loose"})
        db.add_event("wechat_accounts_calibrated",
                     f"用本机微信关注列表校准公众号库：改回 {len(changed)} 条"
                     f"（精确 {len(buckets.get('exact', []))}、折叠 {len(buckets.get('folded', []))}"
                     f"、形近 {len(buckets.get('fuzzy', [])) if args.with_fuzzy else 0}）", None, None)
        out = Path(args.json).with_suffix(".applied.json") if args.json else None
        if out:
            out.write_text(json.dumps(changed, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"改动明细：{out}")
        print(f"\n已写回 {len(changed)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
