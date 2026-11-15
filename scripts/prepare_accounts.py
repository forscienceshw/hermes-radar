import csv
import json
import re
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
INBOX = BASE / "inbox"
FILES = sorted(INBOX.glob("*.jpg"))
ORDER = {path.name: index for index, path in enumerate(FILES)}

UI_NAMES = {
    "公众号", "合集", "最近更新", "最近关注", "星标", "置顶", "搜索",
    "昨天", "前天", "今天", "广告", "更多",
}
DATE_RE = re.compile(r"^(昨天|前天|今天|\d+分钟前|\d+小时前|\d+天前)$")
UPDATE_RE = re.compile(r"(昨天|前天|今天|\d+分钟前|\d+小时前|\d+天前|\d{1,2}月\d{1,2}日|\d{4}年)")


def normalized(name: str) -> str:
    value = unicodedata.normalize("NFKC", name).lower()
    return re.sub(r"[\s\-_·•|,，。.!！?？:：;；()（）\[\]【】]+", "", value)


def name_is_suspicious(row: dict) -> bool:
    name = row["name"].strip()
    if name in UI_NAMES or DATE_RE.match(name):
        return True
    if len(name) < 2 or len(name) > 36:
        return True
    if re.search(r"[。！？；]$", name) and len(name) > 10:
        return True
    if name.count(" ") >= 5:
        return True
    return False


def load_timestamps():
    mapping = {}
    for path in sorted(DATA.glob("account_timestamps_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for image, entries in data.items():
            mapping.setdefault(image, []).extend(entries)
    return mapping


def nearest_update(timestamp_map, image, y):
    candidates = []
    for entry in timestamp_map.get(image, []):
        if abs(entry["y"] - y) <= 55 and UPDATE_RE.search(entry["text"]):
            candidates.append(entry)
    if not candidates:
        return ""
    return min(candidates, key=lambda x: abs(x["y"] - y))["text"]


def load_shards():
    rows = []
    shard_files = sorted(DATA.glob("account_candidates_*.json"))
    # Remove merged files from a previous run so they are not read as shards.
    shard_files = [path for path in shard_files if not path.stem.endswith("_merged")]
    for path in shard_files:
        if path.name == "account_candidates.json":
            continue
        rows.extend(json.loads(path.read_text(encoding="utf-8")))
    return rows


def main():
    rows = load_shards()
    timestamp_map = load_timestamps()
    merged = {}
    rejected = []
    for row in rows:
        if name_is_suspicious(row):
            rejected.append(row)
            continue
        key = normalized(row["name"])
        if not key:
            rejected.append(row)
            continue
        image = row.get("image", "")
        sort_key = (ORDER.get(image, 999), row.get("y", 0))
        row["last_update"] = nearest_update(timestamp_map, image, row.get("y", 0))
        current = merged.get(key)
        if current is None:
            item = dict(row)
            item["images"] = [image] if image else []
            item["sort_key"] = sort_key
            merged[key] = item
            continue
        current["confidence"] = max(current["confidence"], row.get("confidence", 0))
        if len(row.get("description", "")) > len(current.get("description", "")):
            current["description"] = row["description"]
        if image and image not in current["images"]:
            current["images"].append(image)
        if row.get("last_update") and not current.get("last_update"):
            current["last_update"] = row["last_update"]
        current["sort_key"] = min(current["sort_key"], sort_key)

    accounts = sorted(merged.values(), key=lambda x: x["sort_key"])
    for index, item in enumerate(accounts, 1):
        item["id"] = index
        item.pop("sort_key", None)
        item.pop("image", None)
        item.pop("image_index", None)
        item.pop("y", None)
    output = {
        "source_rows": len(rows),
        "unique_accounts": len(accounts),
        "rejected_suspicious_rows": len(rejected),
        "accounts": accounts,
        "rejected": rejected,
    }
    (DATA / "accounts_review.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    with (DATA / "公众号_原始去重清单.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["序号", "公众号名称", "最近文章/简介", "最近更新", "OCR置信度", "建议分类", "建议保留", "分类理由", "你的决定"])
        for item in accounts:
            writer.writerow([item["id"], item["name"], item["description"], item.get("last_update", ""),
                             item["confidence"], "", "", "", ""])
    print(json.dumps({"source_rows": len(rows), "unique": len(accounts), "rejected": len(rejected)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
