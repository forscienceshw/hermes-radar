"""公众号面板上的三个动作：补录、排除、改分类。

排除不是"这篇删掉"这么简单。用户的原话是「排除出去的就自动记住该公众号被我
排除掉可能是因为什么，以后同等原因的就也自动排除掉」——所以这里把一次排除
变成两件事：

* 给这一篇打标记（`wechat_article_marks`），面板上立刻显示成灰色的「已排除」；
* 按原因建一条该号专属的过滤规则（`filter_rules`，scope=account），于是它下次
  再发同类标题，在入库那一刻就被挡住，连索引都不留。

规则是用户自己的东西，面板只负责按预设关键词起草，规则页里能改能删；删规则会
把被它挡住的条目一起放回来（`filters.restore_by_rule` 已有的行为）。
"""
from __future__ import annotations

from typing import Any, Iterable

from ..utils import canonical_url, now_iso
from . import filters

# 排除原因预设。关键词是"同类"的判据：命中标题或简介就算同一类。
# 只给常见的几类，剩下的让用户自己写——硬塞更多分类只会误伤。
EXCLUDE_PRESETS: list[dict[str, Any]] = [
    {"code": "ads", "label": "广告 / 带货",
     "keywords": ["广告", "带货", "优惠", "折扣", "下单", "团购", "抽奖", "福利", "秒杀", "买送"]},
    {"code": "jobs", "label": "招聘 / 招生",
     "keywords": ["招聘", "招募", "招生", "求职", "简历", "博士后", "夏令营", "宣讲", "编制"]},
    {"code": "course", "label": "课程 / 培训",
     "keywords": ["课程", "训练营", "培训班", "直播课", "报名", "学费", "开班", "讲座预告"]},
    {"code": "life", "label": "生活 / 娱乐",
     "keywords": ["养生", "美食", "旅游", "情感", "星座", "八卦", "娱乐", "运势", "搞笑"]},
    {"code": "other", "label": "其他（自己写关键词）", "keywords": []},
]


def _clean(urls: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for value in urls or []:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            canonical = canonical_url(text)
        except ValueError:
            continue
        if canonical not in seen:
            seen.append(canonical)
    return seen


def marks_for(db: Any, canonical_urls: list[str]) -> dict[str, dict[str, Any]]:
    """这批链接里哪些已经被面板排除过。"""
    if not canonical_urls:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(canonical_urls), 300):
        chunk = canonical_urls[start:start + 300]
        rows = db.query(f"SELECT * FROM wechat_article_marks WHERE canonical_url IN ({','.join('?' * len(chunk))})",
                        chunk)
        for row in rows:
            result[str(row["canonical_url"])] = row
    return result


def library_index(db: Any, canonical_urls: list[str]) -> dict[str, dict[str, Any]]:
    """这批链接里哪些已经在雷达里了，以及到了哪一步。"""
    if not canonical_urls:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(canonical_urls), 300):
        chunk = canonical_urls[start:start + 300]
        rows = db.query(f"""SELECT id,canonical_url,title,status,excluded_at,exclusion_reason
            FROM items WHERE kind='article' AND canonical_url IN ({','.join('?' * len(chunk))})""", chunk)
        for row in rows:
            result[str(row["canonical_url"])] = row
    return result


def state_of(canonical: str, library: dict[str, dict[str, Any]],
             marks: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """一张卡片该怎么显示：new / index_only / archived / excluded。"""
    row = library.get(canonical)
    if row:
        if row.get("excluded_at"):
            return "excluded", {"reason": row.get("exclusion_reason") or "已从资料库排除", "item_id": row["id"]}
        if row.get("status") == "discovered":
            return "index_only", {"item_id": row["id"]}
        return "archived", {"item_id": row["id"], "status": row.get("status")}
    mark = marks.get(canonical)
    if mark and mark.get("state") == "excluded":
        return "excluded", {"reason": mark.get("reason") or "已在面板排除"}
    return "new", None


def import_articles(db: Any, account: dict[str, Any], gh_id: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
    """把面板上的推文补进资料库：建条目 + 排队归档，不经过云调用。"""
    created = existed = 0
    for article in articles[:200]:
        url = str(article.get("url") or "").strip()
        if not url:
            continue
        try:
            canonical = canonical_url(url)
        except ValueError:
            continue
        candidate = {
            "kind": "article",
            "url": url,
            "canonical_url": canonical,
            "external_key": f"wechat-panel:{gh_id or account.get('name', '')}:{article.get('message_id') or canonical}",
            "title": str(article.get("title") or "").strip(),
            "author": account.get("name") or "",
            "published_at": article.get("published_at"),
            "metadata": {
                "share_channel": "wechat_public_local",
                "wechat_account_id": gh_id or account.get("account_id") or "",
                "public_message_id": article.get("message_id"),
                "description": article.get("summary") or "",
                "cover": article.get("cover") or "",
                "admission_filter": {"reason": "公众号面板手动补录", "matched_terms": []},
            },
        }
        _, is_new = db.discover_item(candidate)
        created += int(is_new)
        existed += int(not is_new)
        # 补录等于用户明确说「这条我要」：之前被误判排除的顺手收回，没抓过正文的排一次队。
        rows = db.query("""SELECT id,status,html_path,excluded_at FROM items
            WHERE kind='article' AND canonical_url=?""", (canonical,))
        if rows:
            row = rows[0]
            if row["excluded_at"]:
                db.set_items_excluded([row["id"]], "公众号面板补录", excluded=False)
            if not is_new and (row["status"] == "discovered" or not row["html_path"]):
                db.enqueue_job("archive_item", {"version": 1, "item_id": row["id"]},
                               dedupe_key=f"archive:{row['id']}:panel", priority=60)
        db.execute("DELETE FROM wechat_article_marks WHERE canonical_url=?", (canonical,))
    return {"created": created, "existing": existed, "requested": len(articles)}


def restore_articles(db: Any, articles: list[dict[str, Any]]) -> dict[str, Any]:
    """把「已排除」收回：删标记，并把被这批标记挡住/删掉的条目放回资料库。"""
    canonical_urls = _clean(article.get("url") or article.get("canonical_url") for article in articles)
    restored = 0
    for canonical in canonical_urls:
        db.execute("DELETE FROM wechat_article_marks WHERE canonical_url=?", (canonical,))
        rows = db.query("SELECT id,exclusion_reason FROM items WHERE kind='article' AND canonical_url=?",
                        (canonical,))
        for row in rows:
            reason = str(row.get("exclusion_reason") or "")
            if reason.startswith("公众号面板"):
                restored += db.set_items_excluded([row["id"]], "公众号面板恢复", excluded=False)
    return {"restored": restored, "requested": len(canonical_urls)}


def exclude_articles(db: Any, account: dict[str, Any], gh_id: str, articles: list[dict[str, Any]],
                     mode: str = "one", reason: str = "", keywords: list[str] | None = None,
                     account_match: str = "") -> dict[str, Any]:
    """排除。mode=one 只挡这一篇；mode=rule 顺手建一条该号的过滤规则。"""
    canonical_urls = _clean(article.get("url") or article.get("canonical_url") for article in articles)
    if not canonical_urls:
        raise ValueError("没有可排除的链接")
    labels = {canonical: str(article.get("title") or "") for article in articles
              for canonical in [_safe_canonical(article.get("url") or article.get("canonical_url"))] if canonical}
    reason = (reason or "").strip() or "手动排除"
    rule_id: int | None = None
    patterns = [str(word).strip() for word in (keywords or []) if str(word).strip()]
    if mode == "rule" and patterns:
        rule_id = _create_rule(db, account, reason, patterns, gh_id, account_match)
    stamp = now_iso()
    for canonical in canonical_urls:
        db.execute("""INSERT INTO wechat_article_marks
            (canonical_url,account_pk,gh_id,title,state,reason,rule_id,created_at)
            VALUES(?,?,?,?,'excluded',?,?,?)
            ON CONFLICT(canonical_url) DO UPDATE SET state='excluded',reason=excluded.reason,
            rule_id=excluded.rule_id,created_at=excluded.created_at""",
                   (canonical, int(account.get("id") or 0), gh_id, labels.get(canonical, ""),
                    reason, rule_id, stamp))
        rows = db.query("SELECT id FROM items WHERE kind='article' AND canonical_url=?", (canonical,))
        for row in rows:
            db.set_items_excluded([row["id"]], f"公众号面板排除：{reason}")
    hits = 0
    if rule_id:
        rules = [rule for rule in filters.load_rules(db, enabled_only=False) if int(rule["id"]) == rule_id]
        hits = filters.apply_to_existing(db, rules)
    return {"excluded": len(canonical_urls), "rule_id": rule_id, "hit_existing": hits,
            "patterns": patterns, "reason": reason}


def disable_account(db: Any, account_pk: int, reason: str = "") -> dict[str, Any]:
    db.execute("UPDATE wechat_accounts SET enabled=0,updated_at=? WHERE id=?", (now_iso(), account_pk))
    db.add_event("wechat_account_disabled",
                 f"面板里停用了公众号：{reason or '不再收录'}", None, None)
    return {"enabled": 0}


def _safe_canonical(url: Any) -> str:
    try:
        return canonical_url(str(url or ""))
    except ValueError:
        return ""


def _rule_account_name(db: Any, account: dict[str, Any], gh_id: str) -> str:
    """规则里的「公众号」要写成真正出现在条目 author 字段里的那个名字。

    清单里的名字来自 OCR，微信联系人里的可能是备注名，两者差一个字规则就永远
    匹配不上——所以优先用已经收进库里、带这个 gh 编号的条目作者名。
    """
    rows = db.query("""SELECT author,COUNT(*) c FROM items
        WHERE excluded_at IS NULL AND author<>''
          AND json_extract(metadata_json,'$.wechat_account_id')=?
        GROUP BY author ORDER BY c DESC LIMIT 1""", (gh_id,))
    if rows and rows[0].get("author"):
        return str(rows[0]["author"])
    return str(account.get("name") or "")


def _create_rule(db: Any, account: dict[str, Any], reason: str, patterns: list[str],
                 gh_id: str, account_match: str = "") -> int:
    name = (account_match or _rule_account_name(db, account, gh_id)).strip()
    stamp = now_iso()
    db.execute("""INSERT INTO filter_rules(scope,account,field,patterns,match_mode,note,enabled,hits,created_at,updated_at)
        VALUES('account',?,'header',?,'any',?,1,0,?,?)""",
               (name, "、".join(patterns), f"公众号面板排除：{reason}", stamp, stamp))
    row = db.one("SELECT id FROM filter_rules ORDER BY id DESC")
    return int(row["id"]) if row else 0


def rules_for_account(db: Any, account: dict[str, Any], gh_id: str) -> list[dict[str, Any]]:
    """这个号身上现有的排除规则，面板底部直接列出来。"""
    name = _rule_account_name(db, account, gh_id)
    result = []
    for rule in filters.load_rules(db, enabled_only=False):
        if rule.get("scope") != "account":
            continue
        target = str(rule.get("account") or "")
        if name and (target == name or target in name or name in target):
            result.append({"id": int(rule["id"]), "patterns": rule.get("patterns") or "",
                           "note": rule.get("note") or "", "hits": int(rule.get("hits") or 0),
                           "enabled": int(rule.get("enabled") or 0),
                           "describe": filters.describe(rule)})
    return result


def preset_payload() -> dict[str, Any]:
    return {"presets": EXCLUDE_PRESETS}
