"""In-place reading assistant: context assembly, conversations, and notes.

The point of this module is that the user never has to copy background material by
hand.  Every question carries the article, the reading result, the figures already
seen, and whatever text the user selected.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from ..db import Database
from ..utils import head_tail, json_loads, now_iso

STEP_LABELS = (("why", "为什么做"), ("did", "做了什么"), ("saw", "看到什么"), ("means", "说明什么"))


def _compact(text: Any, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def item_context(db: Database, item_id: str, selection: str = "", body_limit: int = 14000) -> str:
    """Render everything a model needs to answer a question about one article."""
    row = db.one("SELECT * FROM items WHERE id=?", (item_id,))
    if not row:
        return ""
    analysis = json_loads(row.get("analysis_json"), {}) or {}
    metadata = json_loads(row.get("metadata_json"), {}) or {}
    parts = [
        f"【文章】{row.get('title') or row.get('url')}",
        f"【来源】{row.get('author') or '未知'} · {row.get('published_at') or row.get('discovered_at') or ''}",
        f"【链接】{row.get('url')}",
        f"【归档完整度】{metadata.get('archive_completeness') or '未知'}",
    ]
    if analysis.get("hook"):
        parts.append(f"【一句话结论】{_compact(analysis['hook'], 400)}")
    if analysis.get("plain_problem"):
        parts.append(f"【研究问题】{_compact(analysis['plain_problem'], 700)}")
    if analysis.get("obstacle"):
        parts.append(f"【此前的障碍】{_compact(analysis['obstacle'], 600)}")
    if analysis.get("core_idea"):
        parts.append(f"【解决思路】{_compact(analysis['core_idea'], 800)}")
    steps = analysis.get("steps") or []
    if isinstance(steps, list) and steps:
        parts.append("【研究步骤】")
        for index, step in enumerate(steps[:10], 1):
            if not isinstance(step, dict):
                continue
            detail = "；".join(f"{label}：{_compact(step.get(key), 260)}"
                               for key, label in STEP_LABELS if step.get(key))
            parts.append(f"{index}. {_compact(step.get('title'), 60)}｜{detail}")
    if analysis.get("answer"):
        parts.append(f"【结论】{_compact(analysis['answer'], 900)}")
    numbers = analysis.get("numbers") or []
    if isinstance(numbers, list) and numbers:
        parts.append("【关键数字】" + "；".join(
            f"{_compact(n.get('value'), 40)}={_compact(n.get('meaning'), 80)}"
            for n in numbers[:12] if isinstance(n, dict)))
    terms = analysis.get("terms") or []
    if isinstance(terms, list) and terms:
        parts.append("【名词】" + "；".join(
            f"{_compact(t.get('term'), 30)}：{_compact(t.get('plain'), 90)}"
            for t in terms[:15] if isinstance(t, dict)))
    if analysis.get("limits"):
        parts.append("【局限】" + "；".join(_compact(x, 180) for x in analysis["limits"][:6]))
    images = metadata.get("images") or []
    if images:
        parts.append(f"【已归档图片】{len(images)} 张（正文中的图注已在全文里；无法查看图片本身）")
    text = (row.get("text") or "").strip()
    if text:
        parts.append("【归档全文节选】\n" + head_tail(text, body_limit))
    if selection.strip():
        parts.append("【用户选中的片段】\n" + head_tail(selection.strip(), 4000))
    return "\n".join(parts)


SYSTEM = """你是 Hermes 科研雷达里的随身讲解助手，读者是生物医学方向的本科生，正在读一篇论文报道。

规则：
1. 直接回答，不要复述问题，不要写"这是一个很好的问题"。
2. 只用提供的材料。材料没有的信息，明确说"归档材料里没有这一项"，不要编造数字或机制。
3. 读者可能不懂术语。第一次出现的关键概念用一句大白话解释。
4. 问细节时给出具体位置或数字（例如"在第三步的测量里"）；问概念时先给一句话定义再展开。
5. 回答控制在 200-400 字；确实需要分点时最多 5 条。不要写总结段。
6. 涉及与原文不同的推断时，句首标注"推断："。"""

SYSTEM_SEARCH = """

【联网检索结果】系统已经替你在用户的电脑上实时检索完毕，结果附在本条消息后面。
7. 你不需要、也不要去检索；**绝对不要说自己"无法联网""无法实时检索""只能使用本次会话附带的资料"**——这些结果就是刚刚检索到的。
8. 可以用它们回答归档材料里没有的内容，但必须区分来源：文章里的说"文章里"，检索到的说"检索到的资料里"。
9. 引用检索结果时在句末标注 [序号]，例如"……（[2]）"。只标真实用到的序号，不要虚构序号。
10. 检索结果与文章结论冲突时，明确指出冲突，不要强行调和。
11. 如果结果确实与问题无关，直接说"这次检索没有查到相关的"并说明检索词是什么，不要说"我无法联网"。"""

OUTPUT_RESERVE = 1600

# 模型名 → 上下文窗口。取各家的保守值，宁小勿大：估小了只是多折叠几轮，
# 估大了会直接把请求打到 400 context_length_exceeded。
CONTEXT_WINDOWS = (
    ("gemini", 1_000_000), ("gpt-4.1", 1_000_000), ("claude", 200_000),
    ("gpt-4o", 128_000), ("qwen", 128_000), ("glm", 128_000), ("kimi", 128_000),
    ("moonshot", 128_000), ("doubao", 128_000), ("yi-", 16384),
    ("deepseek", 64_000), ("llama", 32_000),
)
DEFAULT_WINDOW = 32_000

CJK_CHAR = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    中日韩字符按 0.75 token/字（DeepSeek、Qwen 的实际比率在 0.6–0.7 之间，
    这里留一点余量），其余字符按 4 字符/token，外加每条消息的固定开销。
    """
    value = text or ""
    cjk = len(CJK_CHAR.findall(value))
    return int(cjk * 0.75 + (len(value) - cjk) * 0.29) + 8


def context_window(model: str, configured: int = 0) -> int:
    if configured and configured > 4000:
        return int(configured)
    lowered = (model or "").lower()
    for prefix, window in CONTEXT_WINDOWS:
        if prefix in lowered:
            return window
    return DEFAULT_WINDOW


def _digest_line(entry: dict[str, Any]) -> str:
    content = re.sub(r"\s+", " ", entry.get("content") or "").strip()
    if entry["role"] == "user":
        return f"你问：{content[:180]}"
    return f"我答：{content[:260]}"


def fit_messages(system: str, history: list[dict[str, Any]], question: str, *,
                 budget: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """在 token 预算内组装消息。

    三级降级，代价从低到高：
      1. 折叠更早的对话（抽取式摘要，不额外调模型、不花钱）；
      2. 实在放不下就丢弃最旧的几轮；
      3. 调用方还可以在 provider 报超长时用更小的 budget 重来一次。
    """
    kept = [{"role": e["role"], "content": e["content"]} for e in history
            if e["role"] in {"user", "assistant"} and (e.get("content") or "").strip()]
    folded: list[str] = []
    dropped = 0
    while True:
        digest = ("\n【更早的对话（已折叠，原文仍保存在本地）】\n" + "\n".join(folded[-12:])
                  if folded else "")
        messages = [{"role": "system", "content": system + digest}, *kept,
                    {"role": "user", "content": question}]
        used = int(sum(estimate_tokens(m["content"]) for m in messages) * 1.1)
        if used <= budget:
            break
        if len(kept) > 4:
            # 一次折两条，避免逐条循环太多次。
            for _ in range(min(2, len(kept) - 4)):
                folded.append(_digest_line(kept.pop(0)))
        elif kept:
            kept.pop(0)
            dropped += 1
        else:
            break
    return messages, {"tokens": used, "budget": budget, "folded": len(folded),
                      "dropped": dropped, "messages": len(messages)}


def build_messages(db: Database, chat_id: str, question: str, *, item_id: str | None = None,
                   selection: str = "", history_limit: int = 24, web_block: str = "",
                   model: str = "", configured_window: int = 0,
                   budget_scale: float = 1.0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """组装一次请求，并返回这次实际占了多少上下文。

    先按预算决定正文节选长度，再按剩余额度折叠历史：这样长对话不会崩，
    也不会因为一次塞进 14k 字正文把整段对话挤掉。`budget_scale` 留给
    provider 已经报超长之后的第二次尝试（预算砍半、正文降到最小）。
    """
    window = context_window(model, configured_window)
    budget = max(int((window - OUTPUT_RESERVE) * budget_scale), 3000)
    system_base = SYSTEM + (SYSTEM_SEARCH if web_block else "")
    body_limit = 14000
    system = ""
    # 检索材料现在动辄上万字（抓了正文），必须跟正文一起降级：只缩正文不缩材料，
    # 小窗口模型照样会超；只缩材料不缩正文，模型又看不到当前文章。
    limits = ((14000, 12000), (9000, 9000), (6000, 6000), (3500, 3500), (2000, 2000)) \
        if budget_scale >= 1 else ((2000, 2000),)
    for body_limit, web_cap in limits:
        context = item_context(db, item_id, selection, body_limit=body_limit) if item_id else ""
        system = system_base + ("\n\n以下是当前文章材料：\n" + context if context else "")
        if web_block:
            clipped = web_block if len(web_block) <= web_cap else (
                web_block[:web_cap] + "\n…（检索材料过长，已截断）")
            system += "\n\n" + clipped
        history = messages_for(db, chat_id, history_limit)
        messages, info = fit_messages(system, history, question, budget=budget)
        # 只有"装得下、且没折过历史"才算一步到位；没有历史可折时也要靠缩正文把预算降下来。
        if info["tokens"] <= budget and info["folded"] == 0 and info["dropped"] == 0:
            break
        if body_limit == 2000:
            break
    info.update({"window": window, "body_limit": body_limit,
                 "compressed": bool(info["folded"] or info["dropped"]),
                 "has_search": bool(web_block)})
    return messages, info


def ensure_chat(db: Database, chat_id: str | None = None, *, title: str = "新对话",
                model: str = "", item_id: str | None = None) -> dict[str, Any]:
    if chat_id:
        row = db.one("SELECT * FROM chats WHERE id=?", (chat_id,))
        if row:
            return row
    new_id = chat_id or str(uuid.uuid4())
    timestamp = now_iso()
    db.execute("""INSERT INTO chats(id,title,model,item_id,pinned,created_at,updated_at)
        VALUES(?,?,?,?,0,?,?)""", (new_id, title[:120] or "新对话", model, item_id, timestamp, timestamp))
    return db.one("SELECT * FROM chats WHERE id=?", (new_id,))


def messages_for(db: Database, chat_id: str, limit: int = 40) -> list[dict[str, Any]]:
    rows = db.query("""SELECT role,content,meta_json,created_at FROM chat_messages
        WHERE chat_id=? ORDER BY created_at,id""", (chat_id,))
    return [{"role": row["role"], "content": row["content"],
             "meta": json_loads(row["meta_json"], {}) or {}, "created_at": row["created_at"]} for row in rows][-limit:]


def append_message(db: Database, chat_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> None:
    db.execute("INSERT INTO chat_messages(id,chat_id,role,content,meta_json,created_at) VALUES(?,?,?,?,?,?)",
               (str(uuid.uuid4()), chat_id, role, content, json.dumps(meta or {}, ensure_ascii=False), now_iso()))
    db.execute("UPDATE chats SET updated_at=? WHERE id=?", (now_iso(), chat_id))


def auto_title(question: str) -> str:
    title = re.sub(r"\s+", " ", question).strip()
    return (title[:28] + "…") if len(title) > 28 else (title or "新对话")


def notes_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        item = {"id": row["id"], "item_id": row.get("item_id"), "section": row.get("section") or "",
                "quote": row.get("quote") or "", "body": row.get("body") or "",
                "tags": json_loads(row.get("tags_json"), []) or [], "pinned": bool(row.get("pinned")),
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
                "item_title": row.get("item_title"), "item_url": row.get("item_url")}
        result.append(item)
    return result


def export_markdown(chat: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    lines = [f"# {chat.get('title') or '对话'}", "",
             f"模型 {chat.get('model') or '—'} · 开始 {chat.get('created_at') or ''}", ""]
    for entry in messages:
        who = "我" if entry["role"] == "user" else "Hermes"
        lines.extend([f"## {who}", "", entry["content"].strip(), ""])
    return "\n".join(lines)
