"""Backfill figure captions that WeChat stores in the <img alt> attribute.

The archive used to keep only the downloaded file, so an article whose caption
lives in `alt`（"图 1 两类正交DNA纳米星…"）showed a bare "图1" in the reader.  New
archives record `alt` on the spot; this script fills it in for what is already on
disk, reading the saved raw.html without touching the network.

    python scripts/fill_image_alts.py --dry-run
    python scripts/fill_image_alts.py --apply
"""
from __future__ import annotations

import argparse
import html as html_module
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.db import Database  # noqa: E402
from app.services.archive import raw_file, read_raw_text  # noqa: E402
from app.utils import clean_image_alt, json_loads, stable_json  # noqa: E402

IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""(alt|data-src|data-original|src)\s*=\s*["']([^"']*)["']""", re.I)


def usable_alt(text: str) -> str:
    return clean_image_alt(html_module.unescape(text or ""))


def alts_by_url(raw_html: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for tag in IMG_RE.findall(raw_html):
        attrs = {name.lower(): value for name, value in ATTR_RE.findall(tag)}
        alt = usable_alt(attrs.get("alt") or "")
        if not alt:
            continue
        for key in ("data-src", "data-original", "src"):
            url = html_module.unescape(attrs.get(key) or "").strip()
            if url and not url.startswith("data:"):
                mapping.setdefault(url, alt)
                break
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写库；默认只统计")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库（默认行为）")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    db = Database(BASE / "data" / "radar.db")
    sql = "SELECT id,title,html_path,raw_path,metadata_json FROM items WHERE metadata_json IS NOT NULL"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = db.query(sql)
    touched_items = touched_images = dropped = missing = 0
    for row in rows:
        metadata = json_loads(row["metadata_json"], {}) or {}
        images = [image for image in (metadata.get("images") or []) if image.get("url")]
        if not images:
            continue
        changed = 0
        for image in images:
            # 以前补进来的 "图片13.png"、"公众号固定装饰图3" 要按新规则收回
            if image.get("alt") and not clean_image_alt(image["alt"]):
                image.pop("alt", None)
                dropped += 1
                changed += 1
        needs = [image for image in images if not image.get("alt")]
        if needs:
            snapshot = raw_file(row["raw_path"])
            if not snapshot.is_file():
                missing += 1
            else:
                mapping = alts_by_url(read_raw_text(snapshot))
                for image in needs:
                    alt = mapping.get(image["url"])
                    if alt:
                        image["alt"] = alt
                        changed += 1
        if not changed:
            continue
        touched_items += 1
        touched_images += changed
        if args.apply:
            db.execute("UPDATE items SET metadata_json=? WHERE id=?", (stable_json(metadata), row["id"]))
            sidecar = Path(row["html_path"] or "").with_name("metadata.json")
            if sidecar.is_file():
                sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"+ {changed:2d}  {row['title'][:60]}")
    verb = "已处理" if args.apply else "可处理"
    print(f"{verb} {touched_images} 张图的图注（含清理 {dropped} 条无效 alt），涉及 {touched_items} 篇；"
          f"缺 raw.html 的 {missing} 篇")
    if not args.apply:
        print("加 --apply 才会真正写库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
