import argparse
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

BASE = Path(__file__).resolve().parents[1]
INBOX = BASE / "inbox"
OUT_RAW = BASE / "data" / "account_ocr_raw.json"
OUT_CANDIDATES = BASE / "data" / "account_candidates.json"
LEFT, RIGHT = 170, 1050
BLOCK_HEIGHT, OVERLAP = 1300, 220

STOPWORDS = {
    "公众号", "合集", "最近更新", "最近关注", "昨天", "前天", "今天", "上午", "下午",
    "星标", "置顶", "广告", "更多", "搜索",
}
DATE_RE = re.compile(r"^(昨天|前天|今天|\d+分钟前|\d+小时前|\d+天前|\d{1,2}[:：]\d{2})$")


def item_of(result, top):
    if not result:
        return []
    output = []
    for box, text, score in result:
        xs = [float(point[0]) + LEFT for point in box]
        ys = [float(point[1]) + top for point in box]
        output.append({
            "x": min(xs), "right": max(xs), "y": min(ys), "bottom": max(ys),
            "h": max(ys) - min(ys), "w": max(xs) - min(xs),
            "text": str(text).strip(), "score": float(score),
        })
    return output


def extract_rows(items):
    cleaned = []
    seen = set()
    for item in items:
        text = item["text"].replace(" ", "")
        if not text or item["score"] < 0.38 or item["h"] < 20:
            continue
        if item["x"] < 195 or item["x"] > 1010:
            continue
        if DATE_RE.match(text) or text in STOPWORDS:
            continue
        key = (round(item["y"] / 8), text)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    cleaned.sort(key=lambda x: (x["y"], x["x"]))
    clusters = []
    for item in cleaned:
        if not clusters or item["y"] - clusters[-1][-1]["y"] > 125:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    rows = []
    for cluster in clusters:
        lines = sorted(cluster, key=lambda x: (x["y"], x["x"]))
        name_line = None
        for line in lines:
            text = line["text"]
            if len(text) < 2 or len(text) > 42:
                continue
            if line["x"] > 700:
                continue
            if re.search(r"[。！？；]$", text) and len(text) > 12:
                continue
            name_line = line
            break
        if not name_line:
            continue
        name = name_line["text"].strip(" ·•|")
        if not name or name in STOPWORDS or DATE_RE.match(name):
            continue
        descriptions = []
        for line in lines:
            if line is name_line:
                continue
            delta = line["y"] - name_line["y"]
            if 25 <= delta <= 115 and len(line["text"]) >= 3:
                descriptions.append(line["text"])
        rows.append({
            "name": name,
            "description": " ".join(descriptions),
            "confidence": round(name_line["score"], 4),
            "y": round(name_line["y"]),
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--indices", default="", help="Comma-separated zero-based file indices")
    parser.add_argument("--suffix", default="")
    args = parser.parse_args()
    engine = RapidOCR()
    files = sorted(INBOX.glob("*.jpg"))
    if args.indices:
        wanted = {int(x) for x in args.indices.split(",") if x.strip()}
        files = [file for index, file in enumerate(files) if index in wanted]
    all_rows = []
    raw = {}
    for file_index, path in enumerate(files, 1):
        image = Image.open(path).convert("RGB")
        width, height = image.size
        items = []
        top = 0
        while top < height:
            bottom = min(height, top + BLOCK_HEIGHT)
            crop = image.crop((LEFT, top, min(RIGHT, width), bottom))
            result, _ = engine(np.array(crop))
            items.extend(item_of(result, top))
            if bottom >= height:
                break
            top = bottom - OVERLAP
        rows = extract_rows(items)
        for row in rows:
            row["image"] = path.name
            row["image_index"] = file_index
        all_rows.extend(rows)
        raw[path.name] = {"width": width, "height": height, "items": items, "rows": rows}
        print(f"{file_index}/{len(files)} {path.name}: {len(items)} OCR boxes, {len(rows)} rows", flush=True)
    raw_path = OUT_RAW.with_name(OUT_RAW.stem + (f"_{args.suffix}" if args.suffix else "") + OUT_RAW.suffix)
    candidate_path = OUT_CANDIDATES.with_name(OUT_CANDIDATES.stem + (f"_{args.suffix}" if args.suffix else "") + OUT_CANDIDATES.suffix)
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    candidate_path.write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"TOTAL_ROWS={len(all_rows)}", flush=True)


if __name__ == "__main__":
    main()
