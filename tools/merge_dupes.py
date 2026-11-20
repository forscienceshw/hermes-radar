"""把公众号库里「OCR 残记录 + 后来补进来的真记录」合并掉。

11-13 那批号是从长截图 OCR 出来的：名字被截断、``account_id`` 为空。
11-20 加了「每天同步本机关注列表」以后，同一个号又按 gh 补进来一条真记录，
于是库里成对出现（「Immunology」和「Glyco-Immunology」）。

名字相近不等于同一个号，所以这里的清单是**人工核对过的**，不是算法猜的：

* 前半段靠条目元数据对账——``items.metadata_json.wechat_account_id`` 是入库时
  从链接里取的真 gh，作者署名对得上哪条，说明 OCR 那条就是它；
* 后半段靠 3.x ``PublicMsg.db`` 的卡片反查——OCR 记录里那句简介是当时截屏上的
  标题，拿它去历史消息里搜，命中的号就是它。

用法::

    python tools/merge_dupes.py                 # 只打印将要发生什么
    python tools/merge_dupes.py --apply         # 真合并
    python tools/merge_dupes.py --pair "旧名=新名" --apply

合并只搬 ``wechat_article_marks``（面板里的排除记录）和几个标量字段，
不碰条目正文、不改 author，删掉的那条只是「重复的目录项」。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Database                         # noqa: E402
from app.paths import BACKUP_DIR                    # noqa: E402
from app.services import wechat_panel               # noqa: E402

# (要删掉的 OCR 残记录, 要留下的真记录)
DUPLICATE_PAIRS: list[tuple[str, str]] = [
    # —— 条目元数据直接对上的：作者署名挂在哪个 gh 下，就是那个号 ——
    ("Immunology", "Glyco-Immunology"),
    ("生物材料研究", "生物材料药研社"),
    # —— 3.x 历史卡片反查对上的：简介里的标题只在真号出现过 ——
    ("中国医药教育协会健康管理专", "中国医药教育协会健康管理专委会"),   # 名字被截断
    ("ZhangLab生态水文过程", "ZhangLab生态水文过程 V2"),               # 同一个号换绑
    ("势小馆的智能笔记", "赝势小馆的智能笔记"),                        # 掉了一个「赝」
    ("星使智算", "星使智算 SidereusAI"),                               # 掉了英文后缀
    ("酿美丽研究站", "酝酿美丽研究站"),                                # 掉了一个「酝」
    ("公雨星球", "谷雨星球"),                                          # 公/谷
    ("哔哩哗哩", "哔哩哔哩"),                                          # 哗/哔
    ("看天下", "Vista看天下"),                                         # 掉了英文前缀
    ("人Scholar", "學人Scholar"),                                      # 人/學人
    ("中国人臣公安大兰", "中国人民公安大学"),                          # 整串 OCR 糊了
    ("EnaineerinaEorl ife", "EngineeringForLife"),                     # 整串 OCR 糊了
    ("猎靶客", "Marker Hunter 猎靶客"),                                # 掉了英文前缀
    ("e聚集体", "Aggregate 聚集体"),                                   # 掉了英文前缀
    ("莫尔艺术", "more art 莫尔艺术"),                                 # 掉了英文前缀
    ("a Obscura", "Vita Obscura"),                                     # a/Vita
    ("超然学军", "超然学医"),                                          # 军/医
    ("生物丁程沉思泵", "生物工程沉思录"),                              # 整串 OCR 糊了
    ("面包求职烘焙你的OFFER", "烘焙你的OFFER"),                        # 号改过名
    ("蘇-龍", "蘇-龍 0124"),                                           # 同一个号换绑
    ("草伪天棚", "駒草伪天棚"),                                        # 掉了一个「駒」
    ("支横生物SMO", "南模生物SMOC"),                                   # 南模/支横、SMO/SMOC
]


def resolve(db: Database, pairs: list[tuple[str, str]]) -> tuple[list[tuple[int, int, str, str]], list[str]]:
    """名字 → 主键。名字在库里是 UNIQUE，对不上就如实报出来，不猜。"""
    ready: list[tuple[int, int, str, str]] = []
    missing: list[str] = []
    for drop_name, keep_name in pairs:
        drop = db.one("SELECT id,name,account_id FROM wechat_accounts WHERE name=? COLLATE NOCASE", (drop_name,))
        keep = db.one("SELECT id,name,account_id FROM wechat_accounts WHERE name=? COLLATE NOCASE", (keep_name,))
        if not drop or not keep:
            missing.append(f"{drop_name} → {keep_name}："
                           f"{'旧记录不在' if not drop else ''}{'、' if not drop and not keep else ''}"
                           f"{'真记录不在' if not keep else ''}")
            continue
        if not str(keep["account_id"] or "").strip():
            missing.append(f"{drop_name} → {keep_name}：真记录没有 gh 编号，合并会丢采集能力")
            continue
        ready.append((int(drop["id"]), int(keep["id"]), drop_name, keep_name))
    return ready, missing


def main() -> int:
    parser = argparse.ArgumentParser(description="合并公众号库里的重复记录")
    parser.add_argument("--apply", action="store_true", help="真的写库；不加只打印")
    parser.add_argument("--pair", action="append", default=[],
                        help='临时加一对，格式 "要删的名字=要留的名字"，可重复')
    args = parser.parse_args()

    pairs = list(DUPLICATE_PAIRS)
    for raw in args.pair:
        if "=" not in raw:
            print(f"跳过格式不对的 --pair：{raw}")
            continue
        drop_name, keep_name = (x.strip() for x in raw.split("=", 1))
        pairs.append((drop_name, keep_name))

    db = Database()
    ready, missing = resolve(db, pairs)
    print(f"清单 {len(pairs)} 对，可执行 {len(ready)} 对")
    for drop_pk, keep_pk, drop_name, keep_name in ready:
        print(f"  {drop_name}(#{drop_pk})  →  {keep_name}(#{keep_pk})")
    for line in missing:
        print(f"  [跳过] {line}")

    if not args.apply:
        print("\n只读预览。确认无误后加 --apply 执行。")
        return 0

    # 删除是硬删，先把这几行原样存一份，后悔了还能照原样插回去。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"merge_dupes_{stamp}.json"
    ids = [pk for pair in ready for pk in pair[:2]]
    payload = {
        "accounts": [dict(r) for r in db.query(
            f"SELECT * FROM wechat_accounts WHERE id IN ({','.join('?' * len(ids))})", ids)],
        "marks": [dict(r) for r in db.query(
            f"SELECT * FROM wechat_article_marks WHERE account_pk IN ({','.join('?' * len(ids))})", ids)],
    }
    backup.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"备份：{backup}")

    ok = failed = 0
    for drop_pk, keep_pk, drop_name, keep_name in ready:
        try:
            result = wechat_panel.merge_accounts(db, keep_pk, drop_pk)
        except Exception as exc:                    # noqa: BLE001 - 一条失败不该中断整批
            failed += 1
            print(f"  [失败] {drop_name} → {keep_name}：{exc}")
            continue
        ok += 1
        print(f"  [合并] {result['dropped_name']} → {result['kept_name']}（{result['gh']}），"
              f"搬走排除记录 {result['marks_moved']} 条，enabled={result['enabled']}")
    print(f"\n完成：合并 {ok} 对，失败 {failed} 对")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
