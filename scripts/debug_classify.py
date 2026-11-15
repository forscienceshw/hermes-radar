import asyncio
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from app.services.workbuddy_llm import call_workbuddy
from scripts.classify_accounts import SYSTEM, parse_results

DATA = BASE / "data"
accounts = json.loads((DATA / "accounts_review.json").read_text(encoding="utf-8"))["accounts"]
progress = json.loads((DATA / "account_classification_progress.json").read_text(encoding="utf-8"))
failed = [x for x in accounts if progress.get(str(x["id"]), {}).get("reason") == "规则初判"][:24]
compact = [{"id": x["id"], "name": x["name"], "recent": (x.get("description") or "")[:120]} for x in failed]
prompt = "请分类以下公众号：\n" + json.dumps(compact, ensure_ascii=False)


async def run(model):
    try:
        content = await call_workbuddy([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], model, 240, 0.0)
        results = parse_results(content)
        print(model, "chars", len(content), "results", len(results), "expected", len(compact))
        print(content[:400])
        print(content[-300:])
    except Exception as exc:
        print(model, "ERROR", repr(exc))


async def main():
    await asyncio.gather(run("deepseek-v4.1-flash"), run("glm-5.3-flash"))


asyncio.run(main())
