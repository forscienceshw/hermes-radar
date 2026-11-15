"""Digest rendering and quiet-window scheduling.

Two ideas drive this module:

* A report is only sent inside one of the configured windows, at most
  `max_per_day` times, so the channel stays readable.
* A report has two tiers.  Work that touches protein/RNA templating, nucleic-acid
  nanostructures, aptamers, neuroscience or structural biology gets three sentences
  of substance; everything else is a single scannable line.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ..db import Database
from ..utils import head_tail, json_loads, parse_time

# Ordered: earlier entries are the ones the user asked to read in detail.
FOCUS_RULES: list[tuple[str, str]] = [
    ("蛋白质/酶促核酸合成", r"DNA\s*合成|从头合成|模板合成|末端转移酶|TdT|聚合酶|primerless|template-independent|酶促合成"),
    ("RNA 从头合成", r"RNA\s*合成|随机\s*RNA|OS3|核酶|ribozyme|RNA\s*polymerase|转录组学|RNA\s*writer"),
    ("核酸纳米结构", r"DNA\s*折纸|origami|纳米结构|框架核酸|DNA\s*纳米|四面体|纳米孔|nanopore|分子机器"),
    ("适配体与传感器", r"适配体|aptamer|生物传感|传感器|检测限|比色|荧光探针|电化学传感|CRISPR\s*检测"),
    ("神经科学", r"神经|突触|脑区|皮层|海马|多巴胺|环路|记忆|neuron|synap|cortex|hippocamp"),
    ("结构生物学", r"冷冻电镜|cryo-?EM|晶体结构|结构解析|构象变化|单颗粒|NMR\s*结构|AlphaFold"),
    ("合成生物学", r"合成生物|基因线路|底盘细胞|无细胞|cell-free|定向进化"),
]

TONE_RULES = [
    (r"发现|实现|首次|构建|开发|揭示", "进展"),
]


def _clean(text: Any, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = re.sub(r"^#+\s*", "", value)
    return value[:limit] + ("…" if len(value) > limit else "")


def focus_tags(item: dict[str, Any], analysis: dict[str, Any]) -> list[str]:
    blob = " ".join(str(x or "") for x in (item.get("title"), analysis.get("hook"),
                                            analysis.get("plain_problem"), analysis.get("answer"),
                                            item.get("summary"), item.get("category")))
    return [label for label, pattern in FOCUS_RULES if re.search(pattern, blob, re.I)]


def _brief(item: dict[str, Any], analysis: dict[str, Any], limit: int = 110) -> str:
    title = _clean(item.get("title"), 200)
    for key in ("hook", "one_sentence", "summary"):
        value = analysis.get(key) or (item.get("summary") if key == "summary" else "")
        if not value:
            continue
        text = _clean(value, limit + len(title))
        # Models often open the hook by restating the title; the list already shows it.
        if title and text.startswith(title):
            text = text[len(title):].lstrip("：: ·—-，,")
        return _clean(text, limit)
    return title[:limit]


def classify(db: Database, batch_id: str, threshold: float, digest_limit: int) -> dict[str, Any]:
    rows = db.query("""SELECT i.*,n.score FROM notification_items n JOIN items i ON i.id=n.item_id
        WHERE n.batch_id=? ORDER BY n.score DESC""", (batch_id,))
    highlights: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for item in rows:
        analysis = json_loads(item.get("analysis_json"), {}) or {}
        tags = focus_tags(item, analysis)
        score = float(item.get("importance") or 0)
        # A focused topic needs a decent score; a very high score qualifies on its own.
        if (tags and score >= min(threshold, 70)) or score >= max(threshold, 80):
            item["_focus"] = tags
            item["_analysis"] = analysis
            highlights.append(item)
        else:
            item["_focus"] = tags
            item["_analysis"] = analysis
            rest.append(item)
    return {"highlights": highlights[:2], "rest": rest[:max(1, digest_limit)],
            "total": len(rows), "hidden": max(0, len(rest) - max(1, digest_limit))}


def render_highlights(entries: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(entries, 1):
        analysis = item["_analysis"]
        lines.append(f"**{index}. {_clean(item.get('title'), 62)}**")
        if item["_focus"]:
            lines.append(f"`{' · '.join(item['_focus'])}`")
        if analysis.get("plain_problem"):
            lines.append(f"问题：{_clean(analysis['plain_problem'], 95)}")
        steps = analysis.get("steps") or []
        did = ""
        if isinstance(steps, list) and steps and isinstance(steps[0], dict):
            did = steps[0].get("did") or steps[0].get("why") or ""
        if did:
            lines.append(f"做法：{_clean(did, 95)}")
        if analysis.get("answer"):
            lines.append(f"结论：{_clean(analysis['answer'], 120)}")
        else:
            lines.append(_brief(item, analysis))
        lines.extend([f"[原文]({item['url']})", ""])
    return lines


def render_rest(entries: list[dict[str, Any]], hidden: int) -> list[str]:
    if not entries:
        return []
    raw: dict[str, list[dict[str, Any]]] = {}
    for item in entries:
        raw.setdefault(item.get("category") or "其他", []).append(item)
    # A heading per single item just adds noise; only real groups get one.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for category, items in raw.items():
        grouped[category if len(items) > 1 else "其他"] = grouped.get(
            category if len(items) > 1 else "其他", []) + items
    lines = ["**其余速览**", ""]
    number = 1
    for category, items in grouped.items():
        if category != "其他" or len(grouped) > 1:
            lines.append(f"*{category}*")
        for item in items:
            analysis = item["_analysis"]
            line = f"{number}. {_clean(item.get('title'), 42)} — {_brief(item, analysis, 48)}"
            if item["_focus"]:
                line += f"｜{item['_focus'][0]}"
            lines.append(line)
            number += 1
        lines.append("")
    if hidden:
        lines.append(f"另有 {hidden} 条已在资料库，未列出。")
        lines.append("")
    return lines


def render_digest(db: Database, batch_id: str, settings: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    config = settings["notifications"]
    batch = db.one("SELECT * FROM notification_batches WHERE id=?", (batch_id,))
    if not batch:
        raise ValueError("汇报批次不存在")
    data = classify(db, batch_id, float(config.get("highlight_threshold", 74)), int(config.get("digest_limit", 10)))
    now = datetime.now().astimezone()
    slot = "早报" if now.hour < 11 else "午报" if now.hour < 17 else "晚报"
    lines: list[str] = []
    if data["highlights"]:
        lines.extend([f"**科研雷达 · {now:%m-%d} {slot}｜{data['total']} 条，重点 {len(data['highlights'])} 条**", ""])
        lines.extend(render_highlights(data["highlights"]))
        lines.extend(render_rest(data["rest"], data["hidden"]))
    else:
        lines.extend([f"**科研雷达 · {now:%m-%d} {slot}｜{data['total']} 条速览**", ""])
        lines.extend(render_rest(data["rest"], data["hidden"]) or ["今天没有达到入选线的条目。"])
    lines.append("完整精读、原图与论文都在本机资料库。")
    items = db.query("""SELECT i.*,n.score FROM notification_items n JOIN items i ON i.id=n.item_id
        WHERE n.batch_id=? ORDER BY n.score DESC""", (batch_id,))
    return head_tail("\n".join(lines), 3800), items, batch["channel"]


def next_window(settings: dict[str, Any], now: datetime | None = None) -> tuple[int, datetime]:
    """Seconds until the next allowed send slot."""
    config = settings["notifications"]
    windows = sorted(str(x) for x in (config.get("send_windows") or ["08:30", "13:00", "20:30"]))
    now = now or datetime.now().astimezone()
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        base = now + timedelta(days=day_offset)
        for value in windows:
            try:
                hour, minute = (int(part) for part in value.split(":", 1))
            except ValueError:
                continue
            slot = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # Only future slots.  Returning the slot we are already standing in made
            # the caller compute a 1-second delay and spin: a digest stuck behind the
            # daily cap woke up thousands of times instead of waiting for tomorrow.
            if slot > now and slot.date() == base.date():
                candidates.append(slot)
    target = min(candidates) if candidates else now + timedelta(hours=2)
    return max(1, int((target - now).total_seconds())), target


def window_allows(settings: dict[str, Any], now: datetime | None = None) -> bool:
    config = settings["notifications"]
    now = now or datetime.now().astimezone()
    windows = config.get("send_windows") or []
    if not windows:
        # An empty list is an explicit "send whenever"; it keeps manual flushes and
        # tests from being parked until tomorrow morning.
        return True
    for value in windows:
        try:
            hour, minute = (int(part) for part in str(value).split(":", 1))
        except ValueError:
            continue
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot - timedelta(minutes=5) <= now <= slot + timedelta(minutes=15):
            return True
    return False


def sent_today(db: Database) -> int:
    """Count today's deliveries in local time; sent_at itself is stored in UTC."""
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    rows = db.query("SELECT sent_at FROM notification_batches WHERE state='SENT' AND sent_at IS NOT NULL")
    count = 0
    for row in rows:
        try:
            if parse_time(row["sent_at"]).astimezone().strftime("%Y-%m-%d") == today:
                count += 1
        except (TypeError, ValueError):
            continue
    return count
