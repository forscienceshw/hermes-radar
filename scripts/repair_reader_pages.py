"""Repair archived reader pages that were saved with WeChat's hidden-content style.

Older snapshots kept <div id="js_content" style="visibility:hidden;opacity:0">, so the
offline page showed a title and an empty body. This rewrites only that inline style and
adds a stylesheet guard; it never touches the saved raw snapshot.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from app.paths import DB_PATH  # noqa: E402

GUARD = ('<style>#js_content,[style*="visibility"],[style*="opacity"]'
         '{visibility:visible!important;opacity:1!important}</style>')
HIDDEN = re.compile(r"style\s*=\s*\"[^\"]*(?:visibility\s*:\s*hidden|opacity\s*:\s*0)[^\"]*\"", re.I)


def repair(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "visibility: hidden" not in text and "visibility:hidden" not in text and "opacity: 0" not in text:
        return False
    fixed = HIDDEN.sub('style="visibility:visible;opacity:1"', text)
    if "visibility:visible!important" not in fixed:
        fixed = fixed.replace("</head>", GUARD + "</head>", 1)
    path.write_text(fixed, encoding="utf-8")
    return True


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT html_path FROM items WHERE html_path IS NOT NULL AND html_path!=''").fetchall()
    changed = missing = 0
    for (value,) in rows:
        path = Path(value)
        if not path.is_file():
            missing += 1
            continue
        if repair(path):
            changed += 1
    print(f"检查 {len(rows)} 个离线页：修复 {changed} 个，文件缺失 {missing} 个")


if __name__ == "__main__":
    main()
