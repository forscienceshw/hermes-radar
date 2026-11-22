#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 documents/*/raw.html 压成 raw.html.gz，省磁盘。

raw.html 是抓取时的原始响应，把配图 base64 内嵌在 HTML 里，单篇平均 3.5 MB，
而正文 content.md 只有几十 KB。gzip 能压到 12% 左右，页面本身一个字都不丢。

读取侧已统一走 app.services.archive.raw_file / read_raw_text，压缩后无感。

单进程受磁盘随机 IO 限制只有 23 个/分钟，这里用多进程把队列打深。

    python scripts/compress_raw.py                  # 预览
    python scripts/compress_raw.py --apply          # 执行
    python scripts/compress_raw.py --apply --workers 12
"""
from __future__ import annotations

import argparse
import gzip
import io
import struct
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DOCS = BASE / "data" / "documents"
LEVEL = 3


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def compress_one(path_str: str) -> tuple[int, int, str]:
    """压缩单个文件。返回 (原大小, 压缩后大小, 错误信息)。"""
    p = Path(path_str)
    gz = Path(str(p) + ".gz")
    try:
        data = p.read_bytes()
    except OSError as e:
        return 0, 0, f"{p}: {e}"

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=LEVEL) as g:
        g.write(data)
    packed = buf.getvalue()

    # 用 gzip 尾部自带的 CRC32 + ISIZE 校验，不再解压一遍
    crc, isize = struct.unpack("<II", packed[-8:])
    if crc != (zlib.crc32(data) & 0xFFFFFFFF) or isize != len(data) % (2 ** 32):
        return 0, 0, f"{p}: 校验失败，保留原文件"

    try:
        tmp = Path(str(gz) + ".part")
        tmp.write_bytes(packed)
        tmp.replace(gz)
        p.unlink()
    except OSError as e:
        return 0, 0, f"{p}: 写入失败 {e}"
    return len(data), len(packed), ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正压缩并删除原文件")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（试跑用）")
    ap.add_argument("--workers", type=int, default=8, help="并行进程数")
    args = ap.parse_args()

    if not DOCS.is_dir():
        print(f"找不到 {DOCS}")
        return 2

    files = sorted(DOCS.rglob("raw.html"))
    pending = [str(p) for p in files if not Path(str(p) + ".gz").is_file()]
    already = len(files) - len(pending)
    todo = pending[: args.limit] if args.limit else pending
    planned = sum(Path(p).stat().st_size for p in todo if Path(p).is_file())
    print(f"共 {len(files)} 个 raw.html，待压缩 {len(todo)} 个（已完成 {already} 个）")
    print(f"待压缩体积 {human(planned)}，按 12% 估算可省 {human(planned * 0.88)}")
    if not args.apply:
        print("\n预览模式。确认后加 --apply 执行。")
        return 0
    if not todo:
        print("没有需要处理的文件。")
        return 0

    before = after = done = failed = 0
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, (b, a, err) in enumerate(ex.map(compress_one, todo, chunksize=8), 1):
            if err:
                failed += 1
                print(f"  {err}", file=sys.stderr)
            else:
                before += b
                after += a
                done += 1
            if i % 400 == 0:
                el = time.time() - t0
                rate = i / el * 60 if el else 0
                print(f"  进度 {i}/{len(todo)}  {rate:.0f} 个/分  已省 {human(before - after)}",
                      file=sys.stderr)

    dt = time.time() - t0
    print(f"\n完成 {done} 个，失败 {failed} 个，用时 {dt / 60:.1f} 分钟")
    if before:
        print(f"{human(before)} → {human(after)}，释放 {human(before - after)}"
              f"（{after / before * 100:.1f}%）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
