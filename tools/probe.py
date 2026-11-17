"""微信 4.2 只读探针 —— 集成前先用它确认"这台机器能读"。

放在项目根目录（`app/` 的同级）运行：

    python tools/probe.py status             # 进程 / 库目录 / 库数量
    python tools/probe.py refresh            # 取密钥 + 全量解密（含 WAL 合并）
    python tools/probe.py articles [天数]     # 列出公众号文章（默认 7 天）
    python tools/probe.py links [天数]        # 列出自己转发的文章
    python tools/probe.py accounts [数量]     # 列出关注的全部公众号

每一步都会打印明确的失败原因，不会静默返回空列表。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import wechat4_live as L  # noqa: E402


def status() -> int:
    data = L.quick_status()
    print("Weixin.exe 在跑：", data["running"], "| PID：", data["pids"] or "-")
    print("db_storage 目录 ：", data["storages"] or "没找到")
    print("数据库数量      ：", data["databases"])
    if not data["storages"]:
        print("\n没找到库目录。检查：")
        print("  1) 微信 4.x 是否已登录并保持运行")
        print("  2) 数据路径是否被改过（微信 设置 → 文件管理）")
        print("  3) %APPDATA%\\Tencent\\xwechat\\config\\*.ini 里那行路径是否存在")
        return 2
    if not data["running"]:
        print("\n注意：微信没在跑。已解密过的缓存还能读，但拿不到新消息。")
    return 0


def refresh() -> int:
    started = time.monotonic()
    state = L.refresh()
    print(f"数据目录：{state.storage}")
    print(f"缓存目录：{state.cache}")
    print(f"密钥来源：{state.key_source or '缓存'}")
    print(f"更新 {state.refreshed} 个库，跳过 {state.skipped} 个，失败 {len(state.failed)}"
          f"（{time.monotonic() - started:.1f} 秒）")
    for line in state.failed:
        print("  -", line)
    if not state.keys:
        print("\n一把密钥都没拿到。检查：")
        print("  · 微信是否正在运行（4.x 的密钥只在进程内存里）")
        print("  · 是否 4.1+ 版本（更老的版本密钥是明文 x'<hex>'，藏法不同）")
        print("  · 掩码是否变了（见 01-技术原理.md 第 3 节，可打印 blob 看明文形态）")
        return 3
    return 0 if not state.failed else 1


def articles(days: int) -> int:
    since = int(time.time() - days * 86400)
    rows = L.official_articles(since_ts=since)
    print(f"== 近 {days} 天公众号文章 {len(rows)} 条 ==")
    for item in rows[:40]:
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(item['create_time']))} "
              f"{(item['account'] or '')[:18]:20} {item['title'][:44]}")
        print(f"      {item['url'][:120]}")
    if not rows:
        print("  一条都没有。可能是：这个微信没关注公众号 / 近 N 天没有推送 / ")
        print("  库里 message 表还没写入（先在微信里打开一次「订阅号消息」）")
    return 0


def links(days: int) -> int:
    since = int(time.time() - days * 86400)
    rows = L.self_links(since_ts=since)
    print(f"== 近 {days} 天自己转发的文章 {len(rows)} 条 ==")
    for item in rows[:40]:
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(item['create_time']))} "
              f"{item['title'][:50]}")
        print(f"      {item['url'][:120]}")
    if not rows:
        print("  一条都没有。检查：是否真的给「文件传输助手」或自己发过公众号链接")
    return 0


def accounts(limit: int) -> int:
    rows = L.account_rows()
    print(f"== 关注公众号 {len(rows)} 个（前 {limit}） ==")
    for row in rows[:limit]:
        print(f"  {row['name'][:30]:32} {row['account_id']}  {row['alias']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="微信 4.2 只读探针")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["status", "refresh", "articles", "links", "accounts"])
    parser.add_argument("n", nargs="?", type=int, default=0, help="天数或数量")
    args = parser.parse_args()
    if args.command == "status":
        return status()
    if args.command == "refresh":
        return refresh()
    if args.command == "articles":
        return articles(args.n or 7)
    if args.command == "links":
        return links(args.n or 7)
    return accounts(args.n or 30)


if __name__ == "__main__":
    sys.exit(main())
