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

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..utils import canonical_url, now_iso
from . import filters

IDX_IN_URL = re.compile(r"[?&]idx=(\d+)")

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


def library_articles(db: Any, gh_id: str, name: str = "", days: int = 365,
                     limit: int = 300) -> list[dict[str, Any]]:
    """资料库里已经收录的这个号的推文，按发布时间倒序。

    为什么面板要问资料库：本机微信 4.x 不把公众号正文落盘，3.x 的
    ``PublicMsg.db`` 在升级那天就冻结了，内存盒子又只留最近几百条。只问微信
    的话，列表会永远停在升级/换通道的那一天——右侧小字却还写着最近收录的
    标题，两边对不上。资料库里已经归档的条目反而是最完整的一份。

    返回的字段与 ``wechat4_live.recent_for()`` 对齐，方便上层直接合并。
    """
    if not gh_id and not name:
        return []
    span = max(1, min(3650, int(days or 365)))
    since = (datetime.now(timezone.utc) - timedelta(days=span)).strftime("%Y-%m-%d")
    clauses: list[str] = []
    params: list[Any] = [since]
    if gh_id:
        clauses.append("json_extract(metadata_json,'$.wechat_account_id')=?")
        params.append(gh_id)
    if name:
        clauses.append("author=?")
        params.append(name)
    params.append(max(1, min(2000, int(limit or 300))))
    rows = db.query(f"""SELECT id,url,canonical_url,title,summary,published_at,metadata_json
        FROM items
        WHERE kind='article' AND substr(published_at,1,10)>=? AND ({' OR '.join(clauses)})
        ORDER BY published_at DESC LIMIT ?""", params)
    result: list[dict[str, Any]] = []
    for row in rows:
        url = str(row.get("url") or "")
        if not url:
            continue
        canonical = str(row.get("canonical_url") or "").strip()
        if not canonical:
            try:
                canonical = canonical_url(url)
            except ValueError:
                continue
        try:
            stamp = int(datetime.fromisoformat(str(row.get("published_at"))).timestamp())
        except (TypeError, ValueError):
            stamp = 0
        match = IDX_IN_URL.search(url)
        result.append({
            "canonical_url": canonical,
            "url": url,
            "title": str(row.get("title") or ""),
            "digest": str(row.get("summary") or ""),
            "create_time": stamp,
            "local_id": 0,
            "idx": int(match.group(1)) if match else 1,
            "item_id": row.get("id"),
        })
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
        # 一次群发多篇共用 local_id，键里带上 idx，第 2 篇才不会撞键被吞
        idx = 1
        hit = re.search(r"[?&]idx=(\d+)", url)
        if hit:
            idx = int(hit.group(1))
        key = f"wechat-panel:{gh_id or account.get('name', '')}:{article.get('message_id') or canonical}"
        candidate = {
            "kind": "article",
            "url": url,
            "canonical_url": canonical,
            "external_key": key + (f"#{idx}" if idx > 1 else ""),
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
                db.set_items_excluded([row["id"]], "公众号面板补录", excluded=False, manual=True)
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
                restored += db.set_items_excluded([row["id"]], "公众号面板恢复", excluded=False, manual=True)
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


# 分类合并时谁更"像话"：CORE > OPTIONAL > EXCLUDE。
# 旧的 OCR 记录常被猜成 CORE，新记录是 OPTIONAL，取前者不会丢用户的判断。
_FIT_RANK = {"CORE": 0, "OPTIONAL": 1, "EXCLUDE": 2}


def merge_accounts(db: Any, keep_pk: int, drop_pk: int) -> dict[str, Any]:
    """把一条重复的号并到另一条上，数据一条都不丢。

    典型场景：11-13 的长截图 OCR 造了一批残记录（名字被截断、``account_id`` 为空），
    后来又按微信关注列表补进了真记录。两条是同一个号，只是旧的那条认不出 gh。

    合并规则：
    * **一定并到有 gh 的那条**上：采集靠 gh 认号，留一条没有编号的等于留个空壳。
    * ``enabled`` 取或：旧记录里用户勾过"参与监测"，合并后不该悄悄失效。
    * ``fit`` 取更靠前的一档，``category_code`` 优先取非 OTHER 的那个。
    * ``reason``/``description`` 只填空，不覆盖已有文字。
    * 面板「排除」记在 ``wechat_article_marks.account_pk`` 上，必须一起搬过去。

    条目本身（``items.author`` / ``metadata.wechat_account_id``）**不动**：author 是
    文章页面上的真实署名，拿库里的号名去改只会把溯源改坏。
    """
    keep = db.one("SELECT * FROM wechat_accounts WHERE id=?", (int(keep_pk),))
    drop = db.one("SELECT * FROM wechat_accounts WHERE id=?", (int(drop_pk),))
    if not keep or not drop:
        raise ValueError("合并的两个号必须都存在")
    if int(keep_pk) == int(drop_pk):
        raise ValueError("不能和自己合并")
    keep_gh = str(keep.get("account_id") or "").strip()
    drop_gh = str(drop.get("account_id") or "").strip()
    if not keep_gh and drop_gh:
        keep, drop = drop, keep
        keep_gh = drop_gh
    if not keep_gh:
        raise ValueError("两条都没有 gh 编号，认不出哪个才是真号，先别合并")

    fit = keep.get("fit") if _FIT_RANK.get(str(keep.get("fit")), 9) <= _FIT_RANK.get(str(drop.get("fit")), 9) \
        else drop.get("fit")
    category = keep.get("category_code") or "OTHER"
    if str(category) == "OTHER" and drop.get("category_code"):
        category = drop["category_code"]
    enabled = 1 if (int(keep.get("enabled") or 0) or int(drop.get("enabled") or 0)) else 0
    stamp = now_iso()
    with db.transaction() as conn:
        moved = conn.execute(
            "UPDATE wechat_article_marks SET account_pk=? WHERE account_pk=?",
            (int(keep["id"]), int(drop["id"]))).rowcount
        conn.execute(
            """UPDATE wechat_accounts SET
                fit=?, category_code=?, enabled=?,
                reason=CASE WHEN COALESCE(reason,'')='' THEN ? ELSE reason END,
                description=CASE WHEN COALESCE(description,'')='' THEN ? ELSE description END,
                updated_at=? WHERE id=?""",
            (fit, category, enabled, drop.get("reason") or "", drop.get("description") or "",
             stamp, int(keep["id"])))
        conn.execute("DELETE FROM wechat_accounts WHERE id=?", (int(drop["id"]),))
    result = {"kept": int(keep["id"]), "kept_name": keep.get("name"),
              "dropped": int(drop["id"]), "dropped_name": drop.get("name"),
              "gh": keep_gh, "marks_moved": int(moved or 0), "enabled": enabled}
    db.add_event("wechat_account_merged",
                 f"合并重复公众号：{drop.get('name')} → {keep.get('name')}（{keep_gh}）",
                 None, None)
    return result


def sync_account_rows(db: Any, rows: Iterable[dict[str, Any]], channel: str = "") -> dict[str, int]:
    """把「本机微信关注列表」灌进公众号库：只新增、只补 gh，不改启用状态。

    匹配顺序必须是 **gh 编号 → 名字 → 折叠键**。少任何一档，同一份关注列表
    每同步一次就会给 OCR 错名的号再造一条空壳，库里的重复条目只会越攒越多。

    已存在的记录**不改名**：名字可能是用户认得的写法，微信里读到的备注名不一定
    更好；只有新增的记录才用微信真名。gh 编号一定会补上——采集靠它认号。
    """
    from ..utils import name_match_key

    known_id: dict[str, int] = {}
    known_name: dict[str, list[int]] = {}
    folded: dict[str, list[int]] = {}
    for row in db.query("SELECT id,name,account_id FROM wechat_accounts"):
        pk = int(row["id"])
        gh = str(row["account_id"] or "").strip()
        if gh:
            known_id[gh] = pk
        known_name.setdefault(str(row["name"] or "").casefold(), []).append(pk)
        folded.setdefault(name_match_key(row["name"]), []).append(pk)

    inserted = merged = updated = 0
    stamp = now_iso()
    with db.transaction() as conn:
        for row in rows:
            name = str(row.get("name") or "").strip()
            gh = str(row.get("account_id") or "").strip()
            if not name:
                continue
            pk = known_id.get(gh) if gh else None
            if pk is None:
                hits = known_name.get(name.casefold()) or []
                # 同名多条时只有一条能认，宁可不合并也不要乱配对
                pk = hits[0] if len(hits) == 1 else None
            if pk is None and gh:
                hits = folded.get(name_match_key(name)) or []
                if len(hits) == 1:
                    pk = hits[0]
                    merged += 1
            if pk is not None:
                conn.execute(
                    """UPDATE wechat_accounts SET
                        account_id=CASE WHEN ?<>'' THEN ? ELSE account_id END,
                        description=CASE WHEN COALESCE(description,'')='' THEN ? ELSE description END,
                        updated_at=? WHERE id=?""",
                    (gh, gh, row.get("description") or "", stamp, pk))
                updated += 1
                continue
            cursor = None
            try:
                cursor = conn.execute(
                    """INSERT INTO wechat_accounts
                        (name,account_id,category_code,fit,reason,description,enabled,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,0,?,?)""",
                    (name, gh, "OTHER", "OPTIONAL",
                     "本机微信同步，尚未判断是否要追踪", row.get("description") or "",
                     stamp, stamp))
                inserted += 1
            except sqlite3.IntegrityError:
                # 表的 name 是 UNIQUE COLLATE NOCASE；万一折叠键没重合（Unicode
                # 归一化差异），退回按名字补 gh，别让一次同步整批回滚。
                conn.execute(
                    """UPDATE wechat_accounts SET
                        account_id=CASE WHEN ?<>'' THEN ? ELSE account_id END,updated_at=?
                    WHERE name=? COLLATE NOCASE""", (gh, gh, stamp, name))
                updated += 1
            if cursor is not None and gh and cursor.lastrowid:
                known_id[gh] = int(cursor.lastrowid)
    if inserted or merged:
        db.add_event("wechat_accounts_synced",
                     f"从本机微信同步关注列表（{channel or '本机'}）：新增 {inserted} 个、"
                     f"按名字归并 {merged} 个；新号默认不追踪")
    total = db.one("SELECT COUNT(*) c FROM wechat_accounts")["c"]
    enabled = db.one("SELECT COUNT(*) c FROM wechat_accounts WHERE enabled=1")["c"]
    return {"inserted": inserted, "updated": updated, "merged": merged,
            "total": total, "enabled": enabled}


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


# ---------------------------------------------------------------------------
# 新关注的号：启用时自动分类 + 补录最近推文
# ---------------------------------------------------------------------------
# 分类是**规则**，不是模型：新号只在启用那一刻判一次，判错可以手改；为了这一步
# 去调云模型，会让「点一下启用」变成一次计费调用，不值得。
# 权重：名字命中 3 分，简介/最近标题命中 1 分；低于 2 分保留 OTHER——
# 宁可在界面上说「没有可判依据」，也不要猜一个看起来很像的分类。
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "STRUCT_BIO": ["结构生物", "冷冻电镜", "cryo-em", "晶体学", "蛋白结构", "结构解析"],
    "OMICS_BIOINFO": ["生物信息", "生信", "基因组", "转录组", "蛋白质组", "代谢组",
                      "单细胞", "测序", "多组学", "组学", "bioinformatics"],
    "SYNTH_BIO": ["合成生物", "基因编辑", "crispr", "底盘细胞", "代谢工程", "合成基因"],
    "NEURO": ["神经科学", "神经生物", "脑科学", "突触", "神经元", "认知神经",
              "阿尔茨海默", "帕金森", "neuroscience"],
    "CHEM_BIOL": ["化学生物", "化学探针", "点击化学", "生物正交", "chemical biology"],
    "LIFE_MOLECULAR": ["分子生物", "细胞生物", "蛋白质", "基因表达", "转录", "表观遗传",
                       "免疫学", "微生物", "植物", "动物学", "生理", "生化", "代谢",
                       "干细胞", "发育生物", "细胞死亡", "自噬", "生命科学",
                       "biotech", "biotechnology", "molecular biology"],
    "BIOMED": ["生物医学", "临床", "医学", "疾病", "肿瘤", "癌症", "药物", "药理",
               "治疗", "疫苗", "诊断", "医院", "病理", "护理", "公共卫生", "流行病",
               "抗体", "免疫治疗", "biomed", "medicine", "clinical"],
    "CHEM_MAT": ["化学", "材料", "催化", "高分子", "纳米材料", "电化学", "有机化学",
                 "无机化学", "分析化学", "能源材料", "mof", "chemistry", "materials"],
    "PHYS_MATH": ["物理", "数学", "天文", "宇宙", "量子", "凝聚态", "光学", "统计物理",
                  "physics", "mathematics", "astronomy"],
    "CS_AI": ["人工智能", "机器学习", "深度学习", "计算机", "算法", "编程", "大模型",
              "数据科学", "神经网络", "ai", "machine learning"],
    "ENG_TECH": ["工程", "机械", "电子", "微流控", "器件", "机器人", "自动化", "制造",
                 "engineering", "robotics"],
    "INTERDISCIPLINARY": ["交叉学科", "交叉研究", "跨学科", " interdisciplinary"],
    "SCI_COMM": ["nature", "science", "cell", "期刊", "论文", "文献", "学术", "科研",
                 "顶刊", "综述", "投稿", "影响因子", "中国科学", "科学网", "preprint"],
    "ACADEMIC_TOOL": ["科研工具", "科研服务", "绘图", "统计软件", "试剂", "测序服务",
                      "实验外包", "招聘", "求职", "仪器", "生物公司", "论文润色"],
    "CAMPUS_EDU": ["大学", "学院", "研究生", "招生", "校园", "教务", "教育", "保研",
                   "考研", "博士", "硕士"],
    "SOCIAL_HUMANITIES": ["人文", "社科", "历史", "哲学", "社会", "经济", "法律",
                          "政治", "文化", "艺术"],
    "LIFE_ENTERTAINMENT": ["生活", "美食", "旅游", "动漫", "娱乐", "电影", "音乐",
                           "游戏", "搞笑", "情感", "养生", "母婴", "广告"],
}


def classify_account(name: str, description: str = "",
                     titles: Iterable[str] = ()) -> tuple[str, str]:
    """Return (category_code, reason). 规则判不出来就老实返回 OTHER。"""
    name_text = (name or "").lower()
    body_text = " ".join([description or "", *[str(t or "") for t in titles]]).lower()
    scores: dict[str, int] = {}
    matched: dict[str, list[str]] = {}
    for code, words in CATEGORY_KEYWORDS.items():
        score = 0
        hits: list[str] = []
        for word in words:
            token = word.strip().lower()
            if not token:
                continue
            if token in name_text:
                score += 3
                hits.append(word)
            elif token in body_text:
                score += 1
                hits.append(word)
        if score:
            scores[code] = score
            matched[code] = hits
    if not scores:
        return "OTHER", "名称和简介里没有可判定的主题词"
    best = max(scores, key=lambda code: scores[code])
    if scores[best] < 2:
        return "OTHER", f"只有弱线索（{matched[best][0]}），保留待判断"
    return best, "、".join(matched[best][:4])


def resolve_gh(db: Any, account: dict[str, Any]) -> tuple[str, str]:
    """(gh_id, channel)。顺手把编号补回账户行，后续面板/采集都靠它对号。"""
    from . import wechat4_live, wechat_public, wechat_self_archive
    channel = wechat_self_archive.local_channel()
    gh_id = str(account.get("account_id") or "").strip()
    if channel == "4.x" and not gh_id:
        gh_id = wechat4_live.gh_for_name(account["name"])
    if not gh_id:
        gh_id = wechat_public.gh_id_for(account["name"], gh_id)
    if gh_id and str(account.get("account_id") or "") != gh_id:
        db.execute("UPDATE wechat_accounts SET account_id=?,updated_at=? WHERE id=?",
                   (gh_id, now_iso(), account["id"]))
        account["account_id"] = gh_id
    return gh_id, channel


# ---------------------------------------------------------------------------
# 详情页缓存
# ---------------------------------------------------------------------------
ARTICLE_CACHE_TTL = 900          # 15 分钟
ARTICLE_CACHE_DAYS = 730


def _cache_age(row: dict[str, Any] | None) -> float:
    if not row:
        return 1e9
    try:
        stamp = datetime.fromisoformat(str(row.get("synced_at") or ""))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError):
        return 1e9


def _cache_read(db: Any, account_pk: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM wechat_article_cache WHERE account_pk=?", (account_pk,))
    if not row:
        return None
    try:
        items = json.loads(row.get("payload_json") or "[]")
        meta = json.loads(row.get("meta_json") or "{}")
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    return {"items": items, "meta": meta if isinstance(meta, dict) else {},
            "newest_ts": int(row.get("newest_ts") or 0), "synced_at": row.get("synced_at") or ""}


def _cache_write(db: Any, account_pk: int, items: list[dict[str, Any]], meta: dict[str, Any],
                 signal: int = 0) -> None:
    newest = max([int(item.get("create_time") or 0) for item in items] or [0])
    # 记下这次读到的「最新推送信号」。留的推文可能没有链接（微信只存了会话摘要），
    # 那种信号永远不会体现在 items 里，只比 newest_ts 会让缓存每开一次都作废。
    meta = {**meta, "signal": max(0, int(signal or 0))}
    db.execute("""INSERT INTO wechat_article_cache(account_pk,payload_json,item_count,newest_ts,meta_json,synced_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(account_pk) DO UPDATE SET payload_json=excluded.payload_json,
        item_count=excluded.item_count,newest_ts=excluded.newest_ts,
        meta_json=excluded.meta_json,synced_at=excluded.synced_at""",
               (account_pk, json.dumps(items, ensure_ascii=False), len(items),
                newest, json.dumps(meta, ensure_ascii=False), now_iso()))


def newest_signal(gh_id: str) -> int:
    """比缓存新不新的轻量信号：会话摘要时间 + 内存盒子里该号最新一条。

    不能只靠「15 分钟过期」：用户刚关注一个号、刚收到一条推送，再点开详情
    却看不到，体验就是「同步了但没同步」。这里只读一个 sqlite 行和一份 90 秒
    的内存快照，通常几十毫秒。
    """
    newest = 0
    if not gh_id:
        return newest
    try:
        from . import wechat4_live
        newest = max(newest, int(wechat4_live.session_hint(gh_id).get("at") or 0))
    except Exception:                                # noqa: BLE001
        pass
    try:
        from . import wechat4_memory
        for item in wechat4_memory.recent_for(gh_id, days=30, limit=3):
            newest = max(newest, int(item.get("create_time") or 0))
    except Exception:                                # noqa: BLE001
        pass
    return newest


def _to_panel_item(row: dict[str, Any]) -> dict[str, Any]:
    create_time = int(row.get("create_time") or 0)
    if not create_time and row.get("published_at"):
        try:
            create_time = int(datetime.fromisoformat(str(row["published_at"])).timestamp())
        except (TypeError, ValueError):
            create_time = 0
    return {
        "canonical_url": row.get("canonical_url") or "",
        "url": row.get("url") or "",
        "title": row.get("title") or "(无标题)",
        "summary": (row.get("digest") or row.get("summary") or "")[:160],
        "cover": row.get("cover") or "",
        "published_at": row.get("published_at") or (
            datetime.fromtimestamp(create_time, timezone.utc).isoformat(timespec="seconds")
            if create_time else ""),
        "message_id": row.get("local_id") or row.get("message_id") or 0,
        "idx": int(row.get("idx") or 1),
        "create_time": create_time,
    }


def collect_recent(db: Any, account: dict[str, Any], gh_id: str, channel: str,
                   days: int = ARTICLE_CACHE_DAYS, window: int = 240) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """三路合并：4.x/3.x 微信库 + 内存盒子 + 雷达资料库。"""
    from . import wechat_public
    meta: dict[str, Any] = {"channel": channel, "note": "", "legacy_filled": False,
                            "memory_cards": 0}
    merged: dict[str, dict[str, Any]] = {}
    if channel == "4.x":
        from . import wechat4_live, wechat4_memory
        rows = wechat4_live.recent_for(gh_id, limit=window, days=days)
        try:
            from . import wechat3_public
            legacy = wechat3_public.recent_for(gh_id, limit=window, days=days)
        except Exception:                            # noqa: BLE001
            legacy = []
        meta["legacy_filled"] = bool(legacy)
        try:
            memory = wechat4_memory.recent_for(gh_id, days=days, limit=window)
        except Exception:                            # noqa: BLE001
            memory = []
        meta["memory_cards"] = len(memory)
        known = library_articles(db, gh_id, account["name"], days=days, limit=window)
        for source in (rows, memory, legacy, known):
            for row in source:
                canonical = row.get("canonical_url") or ""
                if canonical:
                    merged.setdefault(canonical, row)
        hint = wechat4_live.session_hint(gh_id)
        meta["hint"] = hint
    else:
        # 3.x 的 recent_articles 单次最多 60 条；缓存要支持翻页就得把前几页取齐。
        # 它是按 TalkerId 走索引的，取 4 页仍然很便宜。
        total_hint = 0
        has_more = False
        try:
            for start in range(0, max(60, window), 60):
                payload = wechat_public.recent_articles(gh_id, limit=60, offset=start, days=days)
                total_hint = max(total_hint, int(payload.get("total") or 0))
                has_more = bool(payload.get("has_more"))
                meta["note"] = payload.get("note") or meta["note"]
                for row in payload.get("items") or []:
                    canonical = row.get("canonical_url") or ""
                    if canonical:
                        merged.setdefault(canonical, row)
                if not has_more:
                    break
        except wechat_public.PublicMsgUnavailable as exc:
            raise RuntimeError(str(exc)) from exc
        meta["total_hint"] = total_hint
        meta["api_has_more"] = has_more
        known = library_articles(db, gh_id, account["name"], days=days, limit=window)
        for row in known:
            merged.setdefault(row.get("canonical_url") or "", row)

    items = [_to_panel_item(row) for row in merged.values() if row.get("canonical_url")]
    items.sort(key=lambda item: item["create_time"], reverse=True)
    hint = meta.get("hint") or {}
    hint_at = int(hint.get("at") or 0) if isinstance(hint, dict) else 0
    if not items:
        if hint_at:
            items.append({
                "canonical_url": f"hint:{gh_id}:{hint_at}", "url": "",
                "title": (hint.get("title") or "").strip() or "(标题已被微信清空)",
                "summary": "微信只保留了这个号的会话摘要，正文和链接没有落盘。",
                "cover": "", "published_at": datetime.fromtimestamp(hint_at, timezone.utc).isoformat(timespec="seconds"),
                "message_id": 0, "idx": 1, "create_time": hint_at, "hint": True,
            })
            meta["note"] = (f"微信在 {hint.get('at_display')} 收到过这个号的推送，"
                            "但客户端只留下会话摘要，正文没有保存到本地。")
        else:
            # 空白不等于坏了：4.x 只在收到推送、或你打开过会话时才把正文落盘。
            # 说清楚原因，免得用户以为「同步了但没显示」是面板的 bug。
            meta["note"] = ("本机微信里还没有这个号的推送记录。微信只在收到推送"
                            "或打开过该号会话时才把文章落盘，等它下次推送后再看。")
    elif hint_at and hint_at > (items[0]["create_time"] or 0) + 60:
        # 有旧卡片，但更近的那条只留在会话摘要里——说清楚，别让人以为列表就这些。
        title = (hint.get("title") or "").strip()
        meta["note"] = (f"更近的一条（{hint.get('at_display')}"
                        + (f"「{title[:36]}」" if title else "")
                        + "）微信没把正文存下来，所以列表里没有它。")
    return items, meta


def _page(items: list[dict[str, Any]], offset: int, limit: int, days: int) -> dict[str, Any]:
    since = int(time.time() - max(1, int(days)) * 86400)
    rows = [item for item in items if not item.get("create_time") or item["create_time"] >= since]
    page = rows[offset:offset + limit]
    for item in page:
        if item.get("hint"):
            item["state"] = "hint"
    return {"items": page, "total": len(rows), "has_more": len(rows) > offset + len(page)}


def recent_articles_for_panel(db: Any, account: dict[str, Any], limit: int = 24,
                              offset: int = 0, days: int = 365,
                              refresh: bool = False) -> dict[str, Any]:
    """详情页唯一入口：缓存优先，信号显示有新推文就自动重读一次。"""
    from . import wechat_public
    gh_id, channel = resolve_gh(db, account)
    base: dict[str, Any] = {
        "account": {"id": account["id"], "name": account["name"], "account_id": gh_id,
                    "category_code": account.get("category_code"), "fit": account.get("fit"),
                    "enabled": int(account.get("enabled") or 0)},
        "total": 0, "offset": offset, "limit": limit, "has_more": False, "items": [],
        "channel": channel, "cached": False, "cache_age": 0, "synced_at": "",
    }
    if not gh_id:
        return {**base, "error": "没能把网页上的号名对到微信内部编号：微信里可能没关注它，或者备注名和清单不一致"}

    cache = _cache_read(db, account["id"])
    signal = newest_signal(gh_id)
    if cache and not refresh:
        seen = max(int(cache["newest_ts"]), int((cache["meta"] or {}).get("signal") or 0))
        fresh = _cache_age(cache) <= ARTICLE_CACHE_TTL and signal <= seen + 5
        if fresh:
            if channel == "3.x" and offset + limit > len(cache["items"]):
                # 缓存只装得下前几页；更深的页码直接问 3.x 库，避免翻到第 5 页变空。
                payload = wechat_public.recent_articles(gh_id, limit=limit, offset=offset, days=days)
                return {**base, "items": payload.get("items") or [],
                        "total": int(payload.get("total") or 0),
                        "has_more": bool(payload.get("has_more")),
                        "cached": True, "cache_age": int(_cache_age(cache)),
                        "synced_at": cache["synced_at"], "note": payload.get("note") or ""}
            page = _page(cache["items"], offset, limit, days)
            return {**base, **page, "cached": True,
                    "cache_age": int(_cache_age(cache)), "synced_at": cache["synced_at"],
                    "note": cache["meta"].get("note") or "",
                    "legacy_filled": bool(cache["meta"].get("legacy_filled")),
                    "memory_cards": int(cache["meta"].get("memory_cards") or 0)}

    try:
        items, meta = collect_recent(db, account, gh_id, channel, days=ARTICLE_CACHE_DAYS)
        _cache_write(db, account["id"], items, meta, signal=signal)
    except Exception as exc:                         # noqa: BLE001
        if cache:
            page = _page(cache["items"], offset, limit, days)
            return {**base, **page, "cached": True, "cache_age": int(_cache_age(cache)),
                    "synced_at": cache["synced_at"],
                    "note": f"这次读取微信失败（{exc}），先显示 {_cache_age(cache)/60:.0f} 分钟前的缓存。",
                    "stale": True}
        raise
    page = _page(items, offset, limit, days)
    return {**base, **page, "cached": False, "cache_age": 0, "synced_at": now_iso(),
            "note": meta.get("note") or "",
            "legacy_filled": bool(meta.get("legacy_filled")),
            "memory_cards": int(meta.get("memory_cards") or 0)}


def backfill_account(db: Any, account: dict[str, Any], limit: int = 10,
                     days: int = 30, refresh: bool = True) -> dict[str, Any]:
    """把一个号最近未收录的推文补进资料库。启用时调用，幂等。"""
    if str(account.get("fit") or "").upper() == "EXCLUDE":
        return {"account": account["name"], "skipped": "fit=EXCLUDE", "created": 0, "existing": 0}
    payload = recent_articles_for_panel(db, account, limit=limit, offset=0, days=days, refresh=refresh)
    items = payload.get("items") or []
    titles = [str(item.get("title") or "") for item in items[:12]]
    articles = [item for item in items if item.get("url")]
    if not articles:
        return {"account": account["name"], "created": 0, "existing": 0, "found": 0,
                "titles": titles,
                "note": payload.get("note") or payload.get("error") or ""}
    result = import_articles(db, account, str(account.get("account_id") or ""), articles)
    return {"account": account["name"], "found": len(articles), "titles": titles, **result}
