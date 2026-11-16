"""用户自定义的入库过滤器：按「公众号 + 关键词」挡住不想看的栏目。

和 relevance.py 的分工要说清楚：
- relevance.py 判的是「这是不是科研内容」——全站统一，与个人口味无关；
- 这里判的是「你想不想看」——同一個号既发《Nature》综述也发《招聘启事》，
  只有你自己知道哪一类不用推给你。

规则只匹配标题或「标题 + 简介」，不读正文。正文里出现一次「招聘」是常事
（论文里写「招聘博士后参与本项目」），拿正文做屏蔽必然误伤。
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..utils import now_iso

FIELD_LABELS = {"title": "只看标题", "header": "标题 + 简介"}
SCOPE_LABELS = {"account": "指定公众号", "all": "所有公众号"}
REASON_PREFIX = "自定义过滤规则"

_SPLIT_RE = re.compile(r"[\n\r|｜,，;；、]+")
_ASCII_RE = re.compile(r"^[a-z0-9 .+-]{1,12}$")


def split_patterns(text: str) -> list[str]:
    """关键词支持换行、逗号、竖线、顿号混着写。"""
    return [part.strip() for part in _SPLIT_RE.split(text or "") if part.strip()]


def _norm(text: Any) -> str:
    """去掉所有空白再小写：公众号里的「招 聘」和「招聘」应当算同一个词。"""
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _hit(pattern: str, haystack: str) -> bool:
    needle = _norm(pattern)
    if not needle:
        return False
    # 纯 ASCII 短词要卡词边界，否则 "AI" 会命中 "said"、"main"。
    if _ASCII_RE.match(needle) and " " not in needle:
        return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack) is not None
    return needle in haystack


def load_rules(db: Any, enabled_only: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM filter_rules"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY (scope='account') DESC, id"
    rules = db.query(sql)
    for rule in rules:
        rule["pattern_list"] = split_patterns(rule.get("patterns") or "")
    return rules


def describe(rule: dict[str, Any]) -> str:
    """一句人话，既能放进屏蔽理由，也能直接显示在规则列表里。"""
    patterns = rule.get("pattern_list") or split_patterns(rule.get("patterns") or "")
    joined = "、".join(patterns[:4]) + ("…" if len(patterns) > 4 else "")
    where = f"「{rule.get('account')}」" if rule.get("scope") == "account" else "所有公众号"
    field = "标题" if rule.get("field", "title") == "title" else "标题或简介"
    join_word = "同时含" if rule.get("match_mode") == "all" else "含"
    if not patterns:
        return f"{where}（规则没有关键词）"
    return f"{where}的{field}{join_word}「{joined}」的文章"


def match(rules: list[dict[str, Any]], author: Any, title: Any, description: Any) -> dict[str, Any] | None:
    """返回第一条命中的规则及命中词；没有命中返回 None。"""
    account = _norm(author)
    head = _norm(title)
    header = head + "\n" + _norm(description)
    for rule in rules:
        patterns = rule.get("pattern_list")
        if patterns is None:
            patterns = split_patterns(rule.get("patterns") or "")
        if not patterns:
            continue
        if rule.get("scope") == "account":
            target = _norm(rule.get("account") or "")
            if not target or target not in account:
                continue
        haystack = header if rule.get("field") == "header" else head
        hits = [pattern for pattern in patterns if _hit(pattern, haystack)]
        if not hits:
            continue
        if rule.get("match_mode") == "all" and len(hits) < len(patterns):
            continue
        return {"rule": rule, "hits": hits}
    return None


def reason_for(rule: dict[str, Any], hits: list[str]) -> str:
    return f"{REASON_PREFIX}#{rule['id']}：{describe(rule)}（命中：{'、'.join(hits[:3])}）"


def apply_to_existing(db: Any, rules: list[dict[str, Any]] | None = None) -> int:
    """把现有条目按规则重扫一遍，只加不删——恢复靠用户手动点。

    只处理还在资料库里的条目；已经被别的理由挡住的不重复记。
    """
    rules = load_rules(db) if rules is None else rules
    if not rules:
        return 0
    rows = db.query("""SELECT id,author,title,metadata_json FROM items
        WHERE excluded_at IS NULL AND metadata_json IS NOT NULL""")
    groups: dict[tuple[int, str], list[str]] = {}
    for row in rows:
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        description = metadata.get("description") or ""
        hit = match(rules, row.get("author"), row.get("title"), description)
        if hit:
            groups.setdefault((int(hit["rule"]["id"]), reason_for(hit["rule"], hit["hits"])), []).append(str(row["id"]))
    changed = 0
    for (rule_id, reason), ids in groups.items():
        blocked = db.set_items_excluded(ids, reason)
        changed += blocked
        if blocked:
            db.execute("UPDATE filter_rules SET hits=hits+?,updated_at=? WHERE id=?",
                       (blocked, now_iso(), rule_id))
    return changed


def restore_by_rule(db: Any, rule_id: int) -> int:
    """删规则时把被它挡住的条目一起放回来，否则用户删了规则也看不见东西。"""
    prefix = f"{REASON_PREFIX}#{rule_id}："
    rows = db.query("SELECT id FROM items WHERE excluded_at IS NOT NULL AND exclusion_reason LIKE ?",
                    (prefix + "%",))
    ids = [str(row["id"]) for row in rows]
    return db.set_items_excluded(ids, "删除过滤规则后恢复", excluded=False) if ids else 0


def preview(db: Any, rule: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    """试跑：这条规则会挡住哪些已经收进来的条目。不写库。"""
    rule = dict(rule)
    rule.setdefault("id", 0)
    rule["pattern_list"] = split_patterns(rule.get("patterns") or "")
    if not rule["pattern_list"]:
        return {"matched": 0, "samples": []}
    rows = db.query("""SELECT id,author,title,metadata_json FROM items
        WHERE excluded_at IS NULL ORDER BY discovered_at DESC LIMIT 20000""")
    count = 0
    samples: list[dict[str, str]] = []
    for row in rows:
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        hit = match([rule], row.get("author"), row.get("title"), metadata.get("description") or "")
        if not hit:
            continue
        count += 1
        if len(samples) < limit:
            samples.append({"id": str(row["id"]), "title": row.get("title") or "",
                            "author": row.get("author") or "", "hits": "、".join(hit["hits"][:3])})
    return {"matched": count, "samples": samples}
