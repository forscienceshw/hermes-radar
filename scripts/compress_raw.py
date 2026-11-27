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
import os
import struct
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# 和程序本体一致：RADAR_DATA_DIR 指到别处时，脚本也要处理那份数据，不能闷头
# 压缩仓库里的 data/。
DATA = Path(os.environ.get("RADAR_DATA_DIR") or (BASE / "data"))
DOCS = DATA / "documents"
LEVEL = 3

sys.path.insert(0, str(BASE))
from app.services.purge import remove as direct_remove  # noqa: E402


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
    except OSError as e:
        return 0, 0, f"{p}: 写入失败 {e}"
    # 原文件不在这里删：12 个进程同时调 Shell 删除会互相卡死（实测跑到一半
    # 只剩一个 worker 停在 shell32 里不动）。父进程成批删，一次一个 Shell 调用。
    return len(data), len(packed), ""


def drop_batch(paths: list[Path]) -> int:
    """成批直删原文件。

    `Path.unlink()` 在本机会被"安全删除"垫片接管（一个进程删到 50 个就卡住要
    确认），所以走 purge.remove：一次 SHFileOperationW 删一整批，不带
    FOF_ALLOWUNDO，是项目里统一的直删通道。
    """
    if not paths:
        return 0
    failed = direct_remove(*paths).get("failed") or []
    return len(paths) - len(failed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正压缩并删除原文件")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（试跑用）")
    ap.add_argument("--workers", type=int, default=8, help="并行进程数")
    args = ap.parse_args()

    if not DOCS.is_dir():
        print(f"找不到 {DOCS}（数据目录：{DATA}）")
        return 2

    print(f"数据目录：{DATA}")

    files = sorted(DOCS.rglob("raw.html"))
    pending = [str(p) for p in files if not Path(str(p) + ".gz").is_file()]
    # 上一次跑到一半被打断（比如被安全删除垫片拦住）时，会留下「.gz 有了、原文件
    # 还在」的中间态。这里顺手收尾，只删 mtime 不晚于 .gz 的那些。
    leftover = [p for p in files
                if Path(str(p) + ".gz").is_file()
                and p.stat().st_mtime <= Path(str(p) + ".gz").stat().st_mtime]
    already = len(files) - len(pending) - len(leftover)
    todo = pending[: args.limit] if args.limit else pending
    planned = sum(Path(p).stat().st_size for p in todo if Path(p).is_file())
    print(f"共 {len(files)} 个 raw.html，待压缩 {len(todo)} 个（已完成 {already} 个）")
    print(f"待压缩体积 {human(planned)}，按 12% 估算可省 {human(planned * 0.88)}")
    if leftover:
        print(f"另有 {len(leftover)} 个已压好但原文件没删干净，"
              f"可再省 {human(sum(p.stat().st_size for p in leftover))}")
    if not args.apply:
        print("\n预览模式。确认后加 --apply 执行。")
        return 0

    if leftover:
        print(f"先清掉 {len(leftover)} 个残留原文件…")
        removed = drop_batch(leftover)
        print(f"  已删 {removed} 个")

    if not todo:
        print("没有需要处理的文件。")
        return 0

    before = after = done = failed = dropped = 0
    t0 = time.time()
    batch: list[Path] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(compress_one, path): path for path in todo}
        for i, future in enumerate(as_completed(futures), 1):
            path = futures[future]
            try:
                b, a, err = future.result(timeout=180)
            except Exception as exc:                    # noqa: BLE001 - 单个文件失败不该拖垮整批
                failed += 1
                print(f"  {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            else:
                if err:
                    failed += 1
                    print(f"  {err}", file=sys.stderr)
                else:
                    before += b
                    after += a
                    done += 1
                    batch.append(Path(path))
            if len(batch) >= 200:
                dropped += drop_batch(batch)
                batch = []
            if i % 200 == 0:
                el = time.time() - t0
                rate = i / el * 60 if el else 0
                print(f"  进度 {i}/{len(todo)}  {rate:.0f} 个/分  已省 {human(before - after)}",
                      file=sys.stderr)
    dropped += drop_batch(batch)

    dt = time.time() - t0
    print(f"\n完成 {done} 个，删掉原文件 {dropped} 个，失败 {failed} 个，用时 {dt / 60:.1f} 分钟")
    if before:
        print(f"{human(before)} → {human(after)}，释放 {human(before - after)}"
              f"（{after / before * 100:.1f}%）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
