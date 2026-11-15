"""Local points ledger.

Every WorkBuddy cloud call already returns a `usage` block containing
`original_credit` (points at the catalogue rate) and `credit` (points actually
deducted).  This module turns the append-only JSONL ledger into the numbers the
dashboard shows, without ever calling the cloud again.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..paths import DATA_DIR

USAGE_LOG = DATA_DIR / "llm-usage.jsonl"


def _credit(usage: dict[str, Any]) -> tuple[float, float]:
    """Return (points actually charged, catalogue reference price).

    The gateway reports the two differently per model: a free model returns
    `credit=0` plus a non-zero `original_credit`, a paid model returns the real
    `credit` and often leaves `original_credit` at zero.  Charging the first and
    keeping the second as reference is the only reading that survives both.
    """
    def number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    charged = number(usage.get("credit"))
    reference = number(usage.get("original_credit"))
    if not charged and not reference:
        # Older rows may only carry token counts; approximate so the chart is never blank.
        charged = reference = number(usage.get("total_tokens")) / 1000
    return charged, reference


def _rows() -> list[dict[str, Any]]:
    if not USAGE_LOG.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with USAGE_LOG.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = entry.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            charged, reference = _credit(usage)
            # Before the task label existed every logged call was a reading.
            task = entry.get("task") or "analyze"
            rows.append({
                "time": entry.get("time") or "",
                "model": entry.get("model") or "",
                "task": task,
                "elapsed_ms": entry.get("elapsed_ms"),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "reasoning_tokens": int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
                "points": round(charged, 4),
                "reference": round(reference, 4),
                "cached_tokens": int(usage.get("prompt_cache_hit_tokens") or usage.get("cached_tokens") or 0),
            })
    rows.sort(key=lambda row: row["time"])
    return rows


def _today_prefix() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def summary(days: int = 30) -> dict[str, Any]:  # noqa: C901 - flat aggregation reads best here
    rows = _rows()
    today = _today_prefix()
    cutoff = (datetime.now().astimezone() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    recent = [row for row in rows if (row["time"] or "") >= cutoff]

    def aggregate(subset: list[dict[str, Any]]) -> dict[str, Any]:
        totals: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "points": 0.0, "reference": 0.0})
        for row in subset:
            bucket = totals[row["task"]]
            bucket["calls"] += 1
            bucket["points"] += row["points"]
            bucket["reference"] += row["reference"]
        return {key: {"calls": int(value["calls"]), "points": round(value["points"], 2),
                      "reference": round(value["reference"], 2)} for key, value in totals.items()}

    by_task = aggregate(recent)
    readings = [row for row in recent if row["task"] == "analyze"]
    chats = [row for row in recent if row["task"] != "analyze"]
    today_rows = [row for row in rows if (row["time"] or "").startswith(today)]

    models: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "points": 0.0, "reference": 0.0, "seconds": 0.0})
    for row in recent:
        bucket = models[row["model"] or "unknown"]
        bucket["calls"] += 1
        bucket["points"] += row["points"]
        bucket["reference"] += row["reference"]
        bucket["seconds"] += (row["elapsed_ms"] or 0) / 1000

    daily: dict[str, float] = defaultdict(float)
    daily_calls: dict[str, int] = defaultdict(int)
    for row in recent:
        day = (row["time"] or "")[:10]
        if day:
            daily[day] += row["points"]
            daily_calls[day] += 1

    def average(subset: list[dict[str, Any]]) -> float:
        return round(sum(row["points"] for row in subset) / len(subset), 2) if subset else 0.0

    return {
        "total_points": round(sum(row["points"] for row in rows), 2),
        "total_reference": round(sum(row["reference"] for row in rows), 2),
        "total_calls": len(rows),
        "today_points": round(sum(row["points"] for row in today_rows), 2),
        "today_calls": len(today_rows),
        "window_days": days,
        "window_points": round(sum(row["points"] for row in recent), 2),
        "by_task": by_task,
        "reading_calls": len(readings),
        "reading_points": round(sum(row["points"] for row in readings), 2),
        "reading_avg": average(readings),
        "reading_median": round(sorted(row["points"] for row in readings)[len(readings) // 2], 2) if readings else 0.0,
        "chat_calls": len(chats),
        "chat_points": round(sum(row["points"] for row in chats), 2),
        "chat_avg": average(chats),
        "by_model": [{"model": key, "calls": int(value["calls"]), "points": round(value["points"], 2),
                      "reference": round(value["reference"], 2),
                      "avg_seconds": round(value["seconds"] / max(1, value["calls"]), 1)}
                     for key, value in sorted(models.items(), key=lambda kv: kv[1]["calls"], reverse=True)],
        "daily": [{"day": day, "points": round(daily[day], 2), "calls": daily_calls[day]}
                  for day in sorted(daily)],
        "paid_share": round(100 * sum(1 for row in recent if row["points"] > 0) / max(1, len(recent)), 1),
    }


def recent_calls(limit: int = 40) -> list[dict[str, Any]]:
    return _rows()[-max(1, limit):][::-1]
