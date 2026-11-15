import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

BASE = Path(__file__).resolve().parents[1]
INBOX = BASE / "inbox"
OUT = BASE / "data" / "account_timestamps.json"
LEFT, RIGHT = 1010, 1240
BLOCK_HEIGHT, OVERLAP = 1300, 180


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--indices", required=True)
    parser.add_argument("--suffix", required=True)
    args = parser.parse_args()
    files = sorted(INBOX.glob("*.jpg"))
    wanted = {int(x) for x in args.indices.split(",") if x.strip()}
    files = [file for index, file in enumerate(files) if index in wanted]
    engine = RapidOCR()
    output = {}
    for path in files:
        image = Image.open(path).convert("RGB")
        width, height = image.size
        entries = []
        top = 0
        while top < height:
            bottom = min(height, top + BLOCK_HEIGHT)
            crop = image.crop((LEFT, top, min(RIGHT, width), bottom))
            result, _ = engine(np.array(crop))
            for box, text, score in result or []:
                ys = [float(point[1]) + top for point in box]
                xs = [float(point[0]) + LEFT for point in box]
                value = str(text).strip()
                if not value or float(score) < 0.4:
                    continue
                if max(ys) - min(ys) < 18:
                    continue
                entries.append({"y": round(min(ys)), "text": value, "score": round(float(score), 3),
                                "x": round(min(xs))})
            if bottom >= height:
                break
            top = bottom - OVERLAP
        deduped = []
        for entry in sorted(entries, key=lambda x: (x["y"], x["x"])):
            if deduped and abs(entry["y"] - deduped[-1]["y"]) < 12 and entry["text"] == deduped[-1]["text"]:
                continue
            deduped.append(entry)
        output[path.name] = deduped
        print(path.name, len(deduped), flush=True)
    OUT.with_name(f"account_timestamps_{args.suffix}.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
