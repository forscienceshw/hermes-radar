"""API-key 模型的 token 账本。

和 `credits.py` 不是一回事：那里记的是 WorkBuddy 点数（`credit` 字段），
这里记的是用户自己在设置里填 API Key 接进来的 OpenAI 兼容模型（DeepSeek、
Kimi、通义……）实际消耗的 prompt/completion token。两者混在一个数字里会
让人以为「点数花得多 = token 用得多」，而免费档恰恰是 token 很多、点数 0。

只记录 provider=openai_compatible。Ollama 是本地推理，不产生 API 费用；
WorkBuddy 点数走 `llm-usage.jsonl`，不要重复记。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..paths import DATA_DIR

API_USAGE_LOG = DATA_DIR / "api-usage.jsonl"


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def record(*, provider: str, model: str, base_url: str = "", usage: dict[str, Any] | None = None,
           elapsed_ms: int = 0, task: str = "analyze", account: str = "", label: str = "") -> None:
    """Append one call. Silently skips providers that do not bill per token."""
    if (provider or "") != "openai_compatible":
        return
    usage = usage if isinstance(usage, dict) else {}
    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    total = _int(usage.get("total_tokens")) or (prompt + completion)
    if not total:
        # 有些兼容端点不回 usage；记一条 0 token 的调用没有意义，还会把
        # 「平均每次」拉低。等它回 usage 再记。
        return
    row = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "provider": provider,
        "model": model or "",
        "base_url": base_url or "",
        "task": task or "analyze",
        "account": account or "",
        "label": label or model or "",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": _int(usage.get("prompt_cache_hit_tokens") or usage.get("cached_tokens")),
        "elapsed_ms": _int(elapsed_ms),
    }
    try:
        API_USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with API_USAGE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        # 记账失败不能影响正在进行的精读/对话。
        pass


def _rows() -> list[dict[str, Any]]:
    if not API_USAGE_LOG.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with API_USAGE_LOG.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            row["total_tokens"] = _int(row.get("total_tokens"))
            row["prompt_tokens"] = _int(row.get("prompt_tokens"))
            row["completion_tokens"] = _int(row.get("completion_tokens"))
            rows.append(row)
    rows.sort(key=lambda item: item.get("time") or "")
    return rows


def summary(days: int = 30) -> dict[str, Any]:
    rows = _rows()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    cutoff = (datetime.now().astimezone() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    recent = [row for row in rows if (row.get("time") or "") >= cutoff]
    today_rows = [row for row in rows if (row.get("time") or "").startswith(today)]

    def tokens(subset: list[dict[str, Any]]) -> int:
        return sum(row["total_tokens"] for row in subset)

    models: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "tokens": 0, "prompt": 0, "completion": 0, "seconds": 0.0,
                 "base_url": "", "label": ""})
    for row in recent:
        key = row.get("model") or row.get("label") or "unknown"
        bucket = models[str(key)]
        bucket["calls"] += 1
        bucket["tokens"] += row["total_tokens"]
        bucket["prompt"] += row["prompt_tokens"]
        bucket["completion"] += row["completion_tokens"]
        bucket["seconds"] += (row.get("elapsed_ms") or 0) / 1000
        bucket["base_url"] = bucket["base_url"] or row.get("base_url") or ""
        bucket["label"] = row.get("label") or bucket["label"]

    daily: dict[str, dict[str, int]] = defaultdict(lambda: {"tokens": 0, "calls": 0})
    for row in recent:
        day = (row.get("time") or "")[:10]
        if day:
            daily[day]["tokens"] += row["total_tokens"]
            daily[day]["calls"] += 1

    return {
        "today_tokens": tokens(today_rows),
        "today_calls": len(today_rows),
        "window_tokens": tokens(recent),
        "window_calls": len(recent),
        "window_days": max(1, days),
        "total_tokens": tokens(rows),
        "total_calls": len(rows),
        "prompt_tokens": sum(row["prompt_tokens"] for row in recent),
        "completion_tokens": sum(row["completion_tokens"] for row in recent),
        "by_model": [{
            "model": key,
            "label": value["label"] or key,
            "base_url": value["base_url"],
            "calls": value["calls"],
            "tokens": value["tokens"],
            "prompt_tokens": value["prompt"],
            "completion_tokens": value["completion"],
            "avg_seconds": round(value["seconds"] / max(1, value["calls"]), 1),
        } for key, value in sorted(models.items(), key=lambda kv: kv[1]["tokens"], reverse=True)],
        "daily": [{"day": day, **value} for day, value in sorted(daily.items())],
    }


def recent_calls(limit: int = 20) -> list[dict[str, Any]]:
    return _rows()[-max(1, limit):][::-1]
