#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""删掉 blobs 里和 papers/<doi>/paper.pdf 完全重复的那一份 PDF。

早期归档把 PDF 同时写进内容寻址的 `data/blobs/xx/<sha>.pdf` 和
`data/papers/<doi>/paper.pdf`，同一份文件占两遍。现在 store_blob_file 已经停了，
这个脚本负责把历史账清掉。

判据只有一条：blob 文件名（内容寻址，等于 sha256）+ 文件大小，都能在
`papers/*/paper.pdf` 里找到同一份，才删。任何对不上的一律保留并打印出来。

    python scripts/dedupe_paper_blobs.py            # 只统计
    python scripts/dedupe_paper_blobs.py --apply    # 真删
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.paths import BLOB_DIR, DATA_DIR  # noqa: E402
from app.services import purge  # noqa: E402

PAPERS = DATA_DIR / "papers"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def sha256_of(path: Path, chunk: int = 4 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正删除重复的 blob")
    args = parser.parse_args()

    blobs = [p for p in BLOB_DIR.rglob("*.pdf") if p.is_file()]
    if not blobs:
        print("blobs 里没有 PDF，不用处理")
        return 0
    blob_sizes = {p.stat().st_size for p in blobs}

    papers = [p for p in PAPERS.rglob("paper.pdf") if p.is_file()]
    # 只对"体积能对上 blob"的论文算 sha，省掉无关的大文件。
    candidates = [p for p in papers if p.stat().st_size in blob_sizes]
    print(f"blob PDF {len(blobs)} 个 / {human(sum(p.stat().st_size for p in blobs))}；"
          f"论文 {len(papers)} 份，其中体积能对上的 {len(candidates)} 份")

    index: dict[str, tuple[Path, int]] = {}
    for i, path in enumerate(candidates, 1):
        try:
            size = path.stat().st_size
            index[sha256_of(path)] = (path, size)
        except OSError as exc:
            print(f"  跳过 {path}: {exc}", file=sys.stderr)
        if i % 200 == 0:
            print(f"  校验论文 {i}/{len(candidates)}", file=sys.stderr)

    doomed: list[Path] = []
    kept_size = 0
    mismatched = 0
    for blob in blobs:
        stem = blob.stem.lower()
        hit = index.get(stem)
        if not hit:
            continue
        paper, size = hit
        if size != blob.stat().st_size:
            mismatched += 1
            print(f"  同名但体积不同，保留：{blob} vs {paper}", file=sys.stderr)
            continue
        doomed.append(blob)
        kept_size += size

    print(f"可删 {len(doomed)} 个，释放 {human(kept_size)}"
          + (f"（另有 {mismatched} 个同名不同体积，已跳过）" if mismatched else ""))
    if not doomed:
        return 0
    if not args.apply:
        print("预览模式。确认后加 --apply 执行。")
        for blob in doomed[:8]:
            print("  -", blob)
        return 0

    removed = 0
    for blob in doomed:
        try:
            purge.unlink(blob)
            removed += 1
        except OSError as exc:
            print(f"  删除失败 {blob}: {exc}", file=sys.stderr)
    print(f"已删除 {removed} 个，释放 {human(kept_size)}")
    return 0 if removed == len(doomed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
