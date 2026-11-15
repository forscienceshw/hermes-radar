"""One-off benchmark: run the new reading prompt on a real item with a chosen model."""
import asyncio
import copy
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from app.db import Database  # noqa: E402
from app.services import analyzer as az  # noqa: E402
from app.services import workbuddy_llm as wl  # noqa: E402
from app.settings import get_settings  # noqa: E402

ITEM = "ac6f1c9b-1944-4bdf-afe9-c7f57ab56467"


async def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else "glm-5.3-flash"
    db = Database()
    settings = copy.deepcopy(get_settings())
    settings["llm"]["model"] = model
    settings["llm"]["timeout_seconds"] = 900
    az.get_settings = lambda: settings  # type: ignore[assignment]
    started = time.time()
    state = {"last": 0}
    original = wl.call_workbuddy_full

    async def traced(messages, model_name, timeout_seconds=240, temperature=0.2,
                     on_progress=None, stall_seconds=None):
        def report(progress):
            elapsed = int(time.time() - started)
            if elapsed - state["last"] >= 15:
                state["last"] = elapsed
                print(f"[{elapsed:4d}s] reasoning={progress.get('reasoningChars', 0)} "
                      f"content={progress.get('contentChars', 0)}", flush=True)
            if on_progress:
                on_progress(progress)
        return await original(messages, model_name, timeout_seconds, temperature, report, stall_seconds)

    wl.call_workbuddy_full = traced  # type: ignore[assignment]
    try:
        await az.analyze_item(db, ITEM, force=True)
    finally:
        wl.call_workbuddy_full = original  # type: ignore[assignment]
    elapsed = int(time.time() - started)
    row = db.one("SELECT status,analysis_json FROM items WHERE id=?", (ITEM,))
    analysis = json.loads(row["analysis_json"])
    print(f"MODEL={model} status={row['status']} elapsed={elapsed}s")
    print(json.dumps(analysis, ensure_ascii=False, indent=1)[:7000])


asyncio.run(main())
