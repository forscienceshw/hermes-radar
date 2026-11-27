"""给一条资料推荐最相关的收藏夹。

为什么不用大模型、也不用向量：试过 bge 相似度，科学文本之间普遍 0.65–0.72，
「分子生物学」和一篇电催化论文也能到 0.70，靠阈值分不开。真正有区分度的是精读
已经产出的 `category` 与 `tags`——它们是模型提炼过的主题词。这里拿收藏夹名字
（支持 `相分离/凝聚体`、`tRNA与非天然氨基酸` 这种多部分名字）去对这些主题词，
命中才算，宁可少推荐也不乱推荐。

推荐结果只用于弹确认框，用户勾选后才写入；自动塞错夹子会让收藏体系慢慢失真。
"""
from __future__ import annotations

import re
from typing import Any

from ..db import Database
from ..utils import json_loads

# 这几个夹子是「什么都能放」的筐，自动推荐没有意义，只会天天弹。
GENERIC = {"实验", "科研杂谈", "分析检验"}


def _norm(value: Any) -> str:
    return re.sub(r"[\s\-_/、，,。.:：;；·（）()\[\]【】]+", "", str(value or "")).lower()


def _flat(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_flat(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_flat(item) for item in value.values())
    return str(value or "")


def _haystack(row: dict[str, Any]) -> tuple[str, str]:
    """返回两份文本：去标点的（中文子串匹配）和空格分词的（英文按整词匹配）。"""
    analysis = json_loads(row.get("analysis_json"), {}) or {}
    parts = [row.get("title"), row.get("summary"), analysis.get("category"),
             analysis.get("summary"), analysis.get("one_sentence"),
             _flat(analysis.get("tags")), _flat(analysis.get("terms"))]
    # 还没精读的条目没有 analysis，但抓取时已按规则打上过 category / tags。
    # 缺了它们，未精读的条目点「智能收藏」永远只能回一句「没找到」。
    if not analysis.get("category"):
        parts.append(row.get("category"))
    if not analysis.get("tags"):
        parts.append(_flat(json_loads(row.get("tags_json"), [])))
    raw = " ".join(str(part) for part in parts if part)
    words = re.sub(r"[^a-z0-9]+", " ", raw.lower())
    return _norm(raw), words


def _hit(part: str, text: tuple[str, str]) -> bool:
    if part.isascii():
        # 英文按整词：`rna` 不能命中 `tRNA` 里的 rna，否则每篇测序文章都会推荐 RNA。
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(part)}(?![a-z0-9])", text[1]))
    return part in text[0]


def _parts(name: str) -> list[str]:
    """`相分离/凝聚体` → 相分离、凝聚体；`tRNA与非天然氨基酸` → tRNA、非天然氨基酸。"""
    values: list[str] = []
    for piece in re.split(r"[/、,，]|与", str(name or "")):
        token = _norm(piece)
        if len(token) >= 2 and token not in values:
            values.append(token)
    whole = _norm(name)
    if len(whole) >= 2 and whole not in values:
        values.append(whole)
    return values


def suggest(db: Database, row: dict[str, Any], existing: list[str], limit: int = 2) -> dict[str, Any]:
    text = _haystack(row)
    if not text[0]:
        return {"suggestions": [], "already": [], "engine": "keywords"}
    rows = db.query("SELECT id,name FROM collections WHERE scope='library'")
    scored: list[dict[str, Any]] = []
    for collection in rows:
        name = str(collection.get("name") or "")
        if name in GENERIC:
            continue
        whole = _norm(name)
        hits = [part for part in _parts(name) if _hit(part, text)]
        if not hits:
            continue
        whole_hit = _hit(whole, text)
        strong = whole_hit or any(len(part) >= 3 for part in hits) or len(hits) >= 2
        if not strong:
            continue
        score = sum(len(part) for part in hits) + (1.0 if whole_hit else 0.0)
        scored.append({"id": collection["id"], "name": name, "score": round(score, 2),
                       "matched": hits[:4]})
    scored.sort(key=lambda item: (-item["score"], item["name"]))
    picked: list[dict[str, Any]] = []
    for item in scored:
        if item["id"] in set(existing):
            continue
        # 第二个建议必须是「同样有把握」的交叉方向；0.7 倍的陪跑项宁可不要。
        if picked and item["score"] < max(3.0, picked[0]["score"] * 0.7):
            break
        picked.append(item)
        if len(picked) >= limit:
            break
    for item in picked:
        item["reason"] = "命中：" + "、".join(item.get("matched") or [])
    # 命中了但已经收藏过的夹子：界面要能说「这篇已经在这里了」，
    # 而不是含糊地说「没找到对得上的」——那会让人以为推荐坏了。
    already = [item["name"] for item in scored if item["id"] in set(existing)]
    return {"suggestions": picked, "already": already, "engine": "keywords"}
