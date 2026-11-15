"""微信 4.x 只读探测：验证能不能取到密钥、能打开哪些库。

用法（在微信 4.x 已登录并运行的状态下）：

    python scripts/wechat4_probe.py                # 只验证解锁
    python scripts/wechat4_probe.py --links 50     # 顺便抓最近 50 条公众号链接

它做的事情和设置页里的「尝试读取 4.x」是同一套代码，只是多了命令行输出，
方便把结果贴出来排查。全程只读：不写微信数据库、不发消息、不改配置。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import wechat4  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="微信 4.x 数据库只读探测")
    parser.add_argument("--links", type=int, default=0, help="顺便抓多少条公众号链接（0=不抓）")
    parser.add_argument("--seconds", type=float, default=25.0, help="内存扫描的时间上限")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    status = wechat4.quick_status()
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print("== 微信 4.x 状态 ==")
        print(f"进程运行：{'是' if status['running'] else '否'}  PID {status['pid'] or '-'}")
        print(f"数据目录：{status['storages'] or '没找到'}")
        print(f"数据库数量：{status['databases']}")

    if not status["running"]:
        print("\n请先登录并保持微信 4.x 运行：密钥只在运行时的进程内存里。")
        return 2
    if not status["storages"]:
        print("\n没找到 xwechat_files/<账号>/db_storage。先在微信里登录一次，再用 4.x 打开「设置 → 文件管理」确认路径。")
        return 2

    result = wechat4.probe(max_seconds=args.seconds)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("\n== 解锁结果 ==")
        if not result.get("ok"):
            print("失败：" + str(result.get("reason")))
            if result.get("scanned_mb"):
                print(f"已扫描 {result['scanned_mb']} MB 内存")
            return 3
        print(f"找到 {result['keys_found']} 个候选密钥，扫描 {result['scanned_mb']} MB 内存")
        print(f"可读库 {len(result['opened'])} 个：")
        for name in result["opened"]:
            print("  +", name)
        if result["failed"]:
            print(f"没打开 {len(result['failed'])} 个（通常是空库或还没写过数据）：")
            for name in result["failed"][:10]:
                print("  -", name)

    if args.links and result.get("ok"):
        storage = Path(result["storage"])
        storage_keys = {name: key for name, key in
                        (wechat4.unlock(storage, wechat4.scan_keys(result["pid"])["keys"])["opened"]).items()}
        links = wechat4.recent_message_links(storage, storage_keys, limit=args.links)
        print(f"\n== 最近公众号链接 {len(links['links'])} 条 ==")
        for item in links["links"][: args.links]:
            print(" ", item["url"][:120], "|", item["source"])
        if links["errors"]:
            print("读取中的问题：")
            for line in links["errors"][:5]:
                print("  -", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
