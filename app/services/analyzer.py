import base64
import io
import logging
import json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..db import Database
from ..net import post_json
from ..settings import get_secret, get_settings
from ..utils import (clean_image_alt, extract_dois, head_tail, json_loads, lexical_score, local_date,
                     now_iso, stable_json)

SCHEMA_HINT = {
    "hook": "≤60字：他们做了什么、发现了什么（不用缩写）",
    "plain_problem": "120-220字：用生活化场景说明这个领域要解决什么问题、为什么值得解决",
    "obstacle": "80-160字：以前卡在哪、为什么难",
    "core_idea": "80-160字：作者的破局思路，为什么能绕开上述障碍",
    "steps": [{"title": "≤14字", "why": "为什么做这一步", "did": "具体做了什么（把操作翻译成动作，术语加括号解释）",
               "saw": "看到了什么（带关键数字或明确趋势）", "means": "这说明什么（一句话）"}],
    "answer": "150-260字：把各步串成因果链，回答问题并说明结论强度",
    "numbers": [{"value": "关键数字", "meaning": "用人话解释它意味着什么"}],
    "terms": [{"term": "术语", "plain": "一句人话解释"}],
    "strengths": ["这项工作真正做对的地方，≤4条"],
    "limits": ["局限，每条一句话，推断需标注"],
    "next_questions": ["具体、可检验的追问"],
    "evidence_gaps": ["缺什么证据"],
    "potential_topics": [{"title": "选题", "rationale": "依据", "first_experiment": "首个实验"}],
    "one_line_takeaway": "≤30字",
    "summary": "100-180字列表摘要：问题-做法-结论",
    "category": "领域分类",
    "tags": ["标签"],
    "relevance_score": "0-100 的整数（满分为 100，不要用 0-10 分制）",
    "importance_score": "0-100 的整数（满分为 100，不要用 0-10 分制）",
    "confidence_note": "full_text / article_only / insufficient",
}

SYSTEM_PROMPT = """你是一位很会讲课的科研讲解者。读者是生物医学方向的本科生，对这篇论文所在的具体领域几乎没有前置知识。
任务不是写审稿意见，而是让读者能用自己的话复述：要解决什么问题、为什么难、作者怎么想到的、每一步做了什么看到了什么、结论是什么、边界在哪。

规则：
1. 先讲懂，再评判。主体是解释；批判只放在 strengths/limits/next_questions，不要混进 steps 和 answer。
2. 不假设读者懂专业名词。第一次出现时用一句人话解释（可放括号），或收进 terms。不要连续堆三个以上未解释的术语。
3. 只能引用【可引用图片】里出现的图号。标注"已附图"的，可以直接描述图里看到的；标注"仅图注"的，只能引用图注说明了什么，不能描述图中细节、颜色或趋势；列表里没有的图号一律不许出现。
4. 不编造数字、样本量、实验条件、统计结果。材料没写就写"报道未给出"。
5. 材料只是简讯、讲不清机制时，confidence_note 填 insufficient，不要用常识补全。
6. 不写"具有重要意义""提供了新思路"这类套话。
7. 不要在推理里逐条自问自答或反复修改措辞。读完材料后直接写出最终 JSON，推理保持简短。
8. 全部用中文书写，不要中英夹杂（专业名词、基因名、材料缩写除外）。

只输出一个合法 JSON 对象，不要 Markdown 代码围栏。字段结构：
""" + json.dumps(SCHEMA_HINT, ensure_ascii=False)

log = logging.getLogger(__name__)

# 图注只认「段落开头的 图N …」。正文里"(图7A)"这种是引用，不是图注；以前按任意位置
# 匹配，会把半句正文挂到图下面，还在 90 字处硬截断，看起来就是"图注没显示全"。
FIGURE_RE = re.compile(
    r"(?:^|\n)[ \t　]*(?:图|fig(?:ure)?\.?)\s*([0-9]{1,2})\s*[　\.、:：\-—]?\s*([^\n]{2,300})",
    re.I)


def _normalise_analysis(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    for key in ("relevance_score", "importance_score"):
        try:
            result[key] = max(0.0, min(100.0, float(result.get(key, 0))))
        except (TypeError, ValueError):
            result[key] = 0.0
    # Models occasionally answer on a 0-10 scale; rescale instead of silently filtering
    # the item.  An old free model returned relevance 3 / importance 2 for a perfectly
    # relevant anatomy post, and the 3 survived every downstream threshold check.
    relevance, importance = result["relevance_score"], result["importance_score"]
    if 0 < max(relevance, importance) <= 10:
        result["relevance_score"], result["importance_score"] = relevance * 10, importance * 10
    elif 0 < relevance <= 10 < importance:
        # Mixed scales in one answer: importance used 0-100 while relevance stayed on
        # 0-10.  Lift only the relevance — a mixed answer is a scale slip far more
        # often than a genuine 3/100, and leaving it silently filters the item.
        result["relevance_score"] = relevance * 10
    for key in ("tags", "strengths", "limits", "next_questions", "evidence_gaps", "potential_topics",
                "limitations", "critical_questions", "idea_connections", "red_flags"):
        if not isinstance(result.get(key), list):
            result[key] = []
    steps = result.get("steps")
    clean_steps: list[dict[str, str]] = []
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, dict):
            clean_steps.append({key: str(step.get(key) or "") for key in ("title", "why", "did", "saw", "means")})
        elif isinstance(step, str) and step.strip():
            clean_steps.append({"title": f"第 {index + 1} 步", "why": "", "did": step, "saw": "", "means": ""})
    result["steps"] = clean_steps
    numbers = []
    for entry in result.get("numbers") if isinstance(result.get("numbers"), list) else []:
        if isinstance(entry, dict) and (entry.get("value") or entry.get("meaning")):
            numbers.append({"value": str(entry.get("value") or ""), "meaning": str(entry.get("meaning") or "")})
    result["numbers"] = numbers
    terms = []
    for entry in result.get("terms") if isinstance(result.get("terms"), list) else []:
        if isinstance(entry, dict) and (entry.get("term") or entry.get("plain")):
            terms.append({"term": str(entry.get("term") or ""), "plain": str(entry.get("plain") or "")})
    result["terms"] = terms
    # Legacy fields keep the digest/notification code working without exposing them in the UI.
    result["limitations"] = result["limitations"] or result["limits"]
    result["critical_questions"] = result["critical_questions"] or result["next_questions"]
    if not result.get("key_findings") and clean_steps:
        result["key_findings"] = [{"finding": f"{s['title']}：{s['means']}".strip("："),
                                   "detail": s["saw"]} for s in clean_steps[:6] if s["title"] or s["means"]]
    result["category"] = str(result.get("category") or "综合科研")
    result["summary"] = str(result.get("summary") or result.get("answer") or result.get("hook") or "")
    result["one_sentence"] = str(result.get("one_line_takeaway") or result.get("hook") or result["summary"][:120])
    if not result.get("research_question"):
        result["research_question"] = str(result.get("plain_problem") or "")
    return result


def _rules_analysis(item: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    text = (item.get("title") or "") + "\n" + (item.get("text") or "")
    relevance = lexical_score(text[:20000], settings["science_keywords"], settings["exclude_keywords"])
    if extract_dois(text):
        relevance = max(relevance, 45.0)
    importance = min(80.0, relevance * 0.7 + (8 if extract_dois(text) else 0) + (5 if len(text) > 3000 else 0))
    categories = [("人工智能", ["人工智能", "大模型", "神经网络", "深度学习"]),
                  ("生物医学", ["细胞", "蛋白", "基因", "临床", "医学", "肿瘤"]),
                  ("物理与天文", ["量子", "粒子", "天体", "宇宙", "物理"]),
                  ("化学与材料", ["化学", "催化", "材料", "分子"])]
    category = next((name for name, words in categories if any(word.lower() in text.lower() for word in words)), "综合科研")
    tags = [word for word in settings["science_keywords"] if word.lower() in text.lower()][:8]
    return _normalise_analysis({
        "summary": "规则模式记录（尚未配置大模型，不是AI精读）：" + head_tail(text, 500),
        "one_sentence": head_tail(item.get("title") or text, 160), "relevance_score": relevance,
        "importance_score": importance, "category": category, "tags": tags,
        "limits": ["当前仅完成本地归档和规则初筛；配置大模型后再生成证据分析、批判问题与idea连接。"],
        "evidence_gaps": ["未进行大模型语义精读"], "analysis_mode": "rules",
    })


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    return {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}


def _relevant_ideas(db: Database, text: str, limit: int) -> list[dict[str, Any]]:
    ideas = db.query("SELECT * FROM ideas WHERE enabled=1 ORDER BY updated_at DESC")
    source = _bigrams(text[:20000])
    ranked = []
    for idea in ideas:
        target = _bigrams((idea["title"] + " " + idea["content"])[:10000])
        overlap = len(source & target) / max(1, len(target))
        ranked.append((overlap, idea))
    selected = [idea for score, idea in sorted(ranked, key=lambda x: x[0], reverse=True) if score > 0.01][:limit]
    if not selected:
        selected = ideas[:min(3, limit)]
    return selected


def _paper_material(db: Database, item_id: str) -> tuple[str, list[dict[str, Any]]]:
    papers = db.query("""SELECT p.* FROM papers p JOIN item_papers ip ON p.id=ip.paper_id WHERE ip.item_id=?""", (item_id,))
    sections = []
    for paper in papers:
        header = f"DOI {paper['doi']}；状态 {paper['status']}；题名 {paper['title']}"
        if paper.get("text_path") and Path(paper["text_path"]).is_file():
            content = Path(paper["text_path"]).read_text(encoding="utf-8", errors="replace")
            sections.append(header + "\n" + head_tail(content, 35000))
        else:
            sections.append(header + f"\n未取得全文：{paper.get('error') or '无开放PDF'}")
    return "\n\n".join(sections), papers


_CAPTION_PREFIX_RE = re.compile(r"^\s*(?:图|fig(?:ure)?\.?)\s*[0-9]{1,2}\s*[　\.、:：\-—]?\s*", re.I)


def _clean_caption(text: str) -> str:
    """Normalise a caption and drop its own 图N prefix (the grid prints the number)."""
    caption = re.sub(r"\s+", " ", str(text or "")).strip()
    return _CAPTION_PREFIX_RE.sub("", caption).strip(" 　.、:：-—｜|，,")


def figure_cards(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Pair archived images with the article's own captions, in reading order.

    Captions are matched only where the article starts a paragraph with 图N, so a
    passing "(图7A)" inside body prose is not mistaken for a caption.  The whole
    paragraph is kept — truncating it at 90 characters was what made long captions
    look cut off in the reader.
    """
    metadata = json_loads(item.get("metadata_json"), {}) or {}
    images = [image for image in (metadata.get("images") or []) if image.get("local_path")]
    captions: dict[int, str] = {}
    for match in FIGURE_RE.finditer(item.get("text") or ""):
        number = int(match.group(1))
        raw = match.group(2)
        # "Fig. 4). Furthermore, when allowed…" 是正文句子，不是图注：去掉编号后以右括号
        # 开头，说明那个括号本来属于"（图4）"。这种宁可退回 alt，也不要贴一段正文上去。
        if raw.lstrip()[:1] in ")]）":
            continue
        caption = _clean_caption(raw)
        if caption and len(caption) >= 4 and number not in captions:
            captions[number] = caption
    cards = []
    for position, image in enumerate(images, 1):
        number = image.get("index", position - 1) + 1
        alt = _clean_caption(clean_image_alt(image.get("alt")))
        caption = captions.get(number, "")
        if not caption or (alt and len(caption) > 240):
            caption = alt or caption
        cards.append({"number": number, "caption": caption,
                      "local_path": image.get("local_path"), "url": image.get("url")})
    return cards


def _visible_figure_text(cards: list[dict[str, Any]], limit: int) -> tuple[str, list[dict[str, Any]]]:
    captioned = [card for card in cards if card.get("caption")]
    if not captioned:
        return "无（全文禁止出现“图N”“如图X所示”这类写法）", []
    attached = captioned[:limit]
    attached_ids = {id(card) for card in attached}
    lines = []
    for card in captioned:
        mark = "已附图" if id(card) in attached_ids else "仅图注"
        lines.append(f"图{card['number']}（{mark}）：{card['caption'][:70]}")
    return "；".join(lines), attached


def _image_content(cards: list[dict[str, Any]], selected: list[dict[str, Any]], max_images: int) -> list[dict[str, Any]]:
    """Send only figures that carry a caption, normalised to a static JPEG.

    Animated GIFs and oversized originals are rejected by the cloud gateway as
    `request_invalid_parameter`, which used to kill an otherwise fine reading.
    """
    from PIL import Image
    result: list[dict[str, Any]] = []
    budget = 9_000_000  # base64 inflates ~4/3; keep the whole request well below the gateway cap
    chosen = {id(card) for card in selected}
    for card in cards:
        if len(result) >= max_images * 2 or budget <= 0:
            break
        if id(card) not in chosen or not card.get("caption"):
            continue
        path = Path(card.get("local_path") or "")
        if not path.is_file():
            continue
        try:
            with Image.open(path) as image:
                image.seek(0)  # animated GIFs must be flattened: the gateway rejects them
                frame = image.convert("RGB")
                frame.thumbnail((1400, 1400))
                buffer = io.BytesIO()
                frame.save(buffer, "JPEG", quality=82, optimize=True)
                data = buffer.getvalue()
        except Exception:
            continue
        if len(data) * 4 // 3 > budget:
            continue
        encoded = base64.b64encode(data).decode("ascii")
        result.append({"type": "text", "text": f"图{card['number']}。{card['caption'][:80]}"})
        result.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        budget -= len(encoded)
    return result


TRIAGE_PROMPT = """下面是一篇文章里按出现顺序抽出的图片缩略图，每张左上角的黑底数字是它的编号（从 1 开始，和正文里的“图1、图2”对应）。
请判断哪些图片不该留在科研资料库里，直接给出编号。

应当删除：
- 广告与推广：课程报名、招聘、征稿、企业宣传、产品促销、活动海报
- 二维码、联系方式、公众号名片、作者头像、引导关注/点赞/在看/转发的截图
- 纯排版装饰：分割线、色块、花边、空白占位、表情包
- 只有栏目名、口号或大标题的横幅，没有任何研究信息
- 与科研无关的实拍照片：会议合影、颁奖照、活动现场、展台、证书奖状、
  参观留念、风景或生活照、人物肖像

应当保留：
- 论文标题截图、期刊文章页截图
- 研究示意图、模式图、实验流程图、器件或仪器原理图
- 数据图、统计图、显微照片、凝胶或测序结果图
- 与论文内容直接相关的图表截图

区分实拍照片和科研图片时看内容：显微镜下、凝胶条带、测序峰图属于科研图片，
即使看起来像照片也要保留；会场、人群、握手、牌匾属于实拍照片，应删除。
拿不准时保留。只输出一个 JSON 对象：{"drop":[编号...],"keep":[编号...],"note":"一句话说明判断依据"}"""


def _label_font(size: int = 26):
    from PIL import ImageFont
    for name in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _contact_sheet(images: list[dict[str, Any]], max_items: int = 20) -> tuple[bytes, list[int]] | None:
    """One numbered thumbnail sheet, so many images can be judged in a single call."""
    from PIL import Image, ImageDraw
    picked: list[tuple[dict[str, Any], Any]] = []
    for image in images[:max_items]:
        path = Path(image.get("local_path") or "")
        if not path.is_file():
            continue
        try:
            with Image.open(path) as handle:
                handle.seek(0)
                frame = handle.convert("RGB")
                frame.thumbnail((250, 250))
                picked.append((image, frame.copy()))
        except Exception:
            continue
    if len(picked) < 2:
        return None
    cell_w, cell_h = 266, 300
    columns = 4
    rows = (len(picked) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    font = _label_font(26)
    indexes: list[int] = []
    for position, (image, thumb) in enumerate(picked):
        left = (position % columns) * cell_w
        top = (position // columns) * cell_h
        sheet.paste(thumb, (left + 8, top + 8))
        draw.rectangle([left + 8, top + 8, left + 62, top + 44], fill=(18, 28, 38))
        # 1-based, exactly how the article text numbers its figures.
        draw.text((left + 18, top + 12), str(int(image.get("index")) + 1), fill=(255, 255, 255), font=font)
        indexes.append(int(image.get("index")))
    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=78, optimize=True)
    return buffer.getvalue(), indexes


async def triage_archived_images(db: Database, item_id: str, force: bool = False) -> int:
    """Ask the vision model which archived images are ads, QR codes or decoration.

    Download-time heuristics already drop page furniture, but a WeChat article often
    embeds its ads as ordinary 832px images (a green title bar, a recruitment card).
    One contact sheet per article is enough to tell those apart from real figures.

    ``force`` re-judges an article that was already triaged, so a user who spots a
    group photo in the library can ask for a second opinion without waiting for a
    re-read.
    """
    settings = get_settings()
    if not settings["llm"].get("vision_enabled") or not settings.get("image_triage_enabled", True):
        return 0
    item = db.one("SELECT * FROM items WHERE id=?", (item_id,))
    if not item or item.get("excluded_at"):
        return 0
    metadata = json_loads(item.get("metadata_json"), {}) or {}
    if metadata.get("image_triage") and not force:
        return 0
    images = [image for image in (metadata.get("images") or []) if image.get("local_path")]
    if len(images) < 3:
        return 0
    sheet = _contact_sheet(images, int(settings.get("image_triage_max", 20)))
    if not sheet:
        return 0
    data, indexes = sheet
    encoded = base64.b64encode(data).decode("ascii")
    messages = [
        {"role": "system", "content": "你是科研资料库的图片管理员，只保留有科研信息价值的图片。"},
        {"role": "user", "content": [
            {"type": "text", "text": TRIAGE_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
        ]},
    ]
    try:
        response = await _llm_call(db, messages, item_id)
        payload = _parse_json(response["content"])
    except Exception as exc:
        # Triage is an optimisation; a failed sheet must never block the reading.
        log.warning("图片分诊失败，先保留全部图片：%s", exc)
        return 0
    raw_drop = payload.get("drop") if isinstance(payload, dict) else None
    keep: set[int] = set()
    for value in (payload.get("keep") or []) if isinstance(payload, dict) else []:
        try:
            keep.add(int(value))
        except (TypeError, ValueError):
            continue
    wanted: list[int] = []
    for value in raw_drop or []:
        try:
            label = int(value)
        except (TypeError, ValueError):
            continue
        # The sheet is labelled from 1; metadata indexes are 0-based.
        if label < 1 or (label - 1) not in indexes:
            continue
        if label not in wanted:
            wanted.append(label)
    # The article's "图1/图2" numbering follows its captions, not the position of an
    # <img> in the page: a promo banner above the first figure is image 1 but is never
    # "图1".  Protecting by that numbering therefore shielded exactly the banners this
    # pass exists to remove (a 27-image article reported "1-4 are ads" and kept them),
    # so the model's visual verdict wins.  Only the explicit keep list is honoured, and
    # a floor keeps an article from losing every illustration.
    labels = [label for label in wanted if label not in keep]
    # Keep at least one image so an article never ends up with an empty figure strip.
    labels = labels[: max(0, len(images) - 1)]
    note = str(payload.get("note") or "")[:300]
    if not labels:
        # Remember that the check ran: otherwise every re-reading pays for the same
        # contact sheet again and again.
        fresh = db.one("SELECT metadata_json FROM items WHERE id=?", (item_id,))
        metadata = json_loads(fresh.get("metadata_json") if fresh else "", {}) or {}
        metadata["image_triage"] = {"dropped": 0, "note": note, "at": now_iso()}
        db.execute("UPDATE items SET metadata_json=? WHERE id=?", (stable_json(metadata), item_id))
        return 0
    from .archive import drop_archived_images
    return drop_archived_images(db, item_id, [label - 1 for label in labels], note)


class DailyLimitReached(RuntimeError):
    """本地每日调用保险丝打满。

    这不是「这篇读坏了」，是「今天排不上了」。以前它和真正的失败共用一条
    路径，重试四次后条目被标成 error，第二天额度恢复了也不会再读——一场
    额度用尽就能把几百篇真文章判死。现在它带着「何时可以再排」一起抛出，
    由 worker 原样顺延到次日，不烧重试次数、不改条目状态。
    """

    def __init__(self, limit: int, message: str, retry_at: str):
        super().__init__(message)
        self.limit = limit
        self.retry_at = retry_at          # ISO 时间：本地次日 0 点后错峰几分钟


def next_local_reset(spread_seconds: int = 900) -> str:
    """本地次日 0 点之后的一个随机时刻。

    保险丝按本地日期归零，但如果几百个任务都定在 00:00:00，第二天一开工
    就是一场雪崩。错峰 15 分钟，够 8 个并发把队列顺开。
    """
    now = datetime.now().astimezone()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour == 0 and now.minute < 15:
        # 刚过零点还在同一批：别把今天的任务推到明天
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    moment = midnight + timedelta(seconds=random.uniform(0, max(0, spread_seconds)))
    return moment.isoformat(timespec="seconds")


def _limit_error(limit: int) -> DailyLimitReached:
    return DailyLimitReached(
        limit,
        f"今天的 {limit} 次本地精读额度已用完，剩余条目自动顺延到明天，不占重试次数",
        next_local_reset())


def ensure_daily_budget(db: Database) -> None:
    """只检查、不计数。

    放在 analyze 任务的最前面：额度已经满了就别再去抓正文、也别再调视觉，
    整单直接顺延。以前是先抓完正文、走完图片分诊才在最后一步撞上保险丝，
    白花流量，还把「没额度」写成了「这篇坏了」。
    """
    limit = int(get_settings()["llm"]["daily_call_limit"])
    row = db.one("SELECT value FROM runtime WHERE key=?", ("llm_calls:" + local_date(),))
    count = int(row["value"]) if row else 0
    if count >= limit:
        raise _limit_error(limit)


def _check_daily_limit(db: Database) -> None:
    limit = int(get_settings()["llm"]["daily_call_limit"])
    key = "llm_calls:" + local_date()
    with db.transaction() as conn:
        row = conn.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
        count = int(row["value"]) if row else 0
        if count >= limit:
            raise _limit_error(limit)
        timestamp = now_iso()
        conn.execute("INSERT INTO runtime(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=?,updated_at=?",
                     (key, str(count + 1), timestamp, str(count + 1), timestamp))


LIMIT_BLOCKED_MARK = "额度已用完"
PARKED_MARK = "精读额度已用完"


def daily_budget_left(db: Database) -> int:
    """今天还剩几次本地精读额度。"""
    limit = int(get_settings()["llm"]["daily_call_limit"])
    row = db.one("SELECT value FROM runtime WHERE key=?", ("llm_calls:" + local_date(),))
    return max(0, limit - (int(row["value"]) if row else 0))


def resume_budget_parked(db: Database, batch: int = 40) -> int:
    """额度被调高后，把顺延到明天的任务提前放回来。

    否则用户上午把上限从 1200 调到 5000，队列还是干等到零点才动——设置改了
    却看不到任何变化，和没改一样。每次只放一批，让队列平滑地铺开，不要一次
    把几百条全推成 READY 去撞服务端限流。
    """
    if daily_budget_left(db) <= 0:
        return 0
    rows = db.query("""SELECT id FROM jobs WHERE kind='analyze_item' AND state='RETRY_WAIT'
        AND run_at>? AND (last_error LIKE ? OR last_error LIKE '%保险丝%')
        ORDER BY run_at LIMIT ?""", (now_iso(), f"%{PARKED_MARK}%", batch))
    stamp = now_iso()
    for row in rows:
        db.execute("UPDATE jobs SET state='READY',run_at=?,last_error=NULL,updated_at=? WHERE id=?",
                   (stamp, stamp, row["id"]))
    return len(rows)


def requeue_limit_blocked(db: Database) -> int:
    """把「因为额度用尽被判死」的条目放回队列。

    历史遗留：保险丝打满曾被当成普通失败，重试四次后条目进了 error，第二天
    额度恢复了也没有人再读它。这里只捞 error 文本里明确提到保险丝/额度的那
    批，别的原因失败的条目一律不动。
    """
    rows = db.query("""SELECT id FROM items WHERE status='error' AND excluded_at IS NULL
        AND (error LIKE '%保险丝%' OR error LIKE ?)""", (f"%{LIMIT_BLOCKED_MARK}%",))
    requeued = 0
    for row in rows:
        alive = db.one("""SELECT id FROM jobs WHERE kind='analyze_item'
            AND state IN ('READY','RETRY_WAIT','LEASED')
            AND json_extract(payload_json,'$.item_id')=?""", (row["id"],))
        if alive:
            continue
        db.execute("UPDATE items SET status='discovered',error=NULL WHERE id=?", (row["id"],))
        db.enqueue_job("analyze_item", {"version": 1, "item_id": row["id"]},
                       dedupe_key=f"analyze:{row['id']}:requeue", priority=60, max_attempts=4)
        requeued += 1
    if requeued:
        db.add_event("limit_requeue", f"上次把额度用尽误判成失败的 {requeued} 条，已放回精读队列")
    return requeued


def _progress_sink(db: Database, item_id: str) -> Callable[[dict[str, Any]], None]:
    state = {"last": 0.0}

    def write(progress: dict[str, Any]) -> None:
        now = time.monotonic()
        if now - state["last"] < 1.5:
            return
        state["last"] = now
        elapsed = int(now - progress.get("started", now))
        db.set_runtime(f"progress:{item_id}", json.dumps({
            "phase": "analyzing", "reasoning": progress.get("reasoningChars", 0),
            "content": progress.get("contentChars", 0), "elapsed": elapsed, "at": now_iso(),
        }, ensure_ascii=False))

    return write


async def _llm_call(db: Database, messages: list[dict[str, Any]], item_id: str = "",
                    material_chars: int = 0) -> dict[str, Any]:
    settings = get_settings()["llm"]
    provider = settings.get("provider", "openai_compatible")
    endpoint = settings["base_url"].rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    if provider == "workbuddy_cloud":
        key = ""
    elif provider == "ollama":
        from urllib.parse import urlsplit
        parts = urlsplit(endpoint)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"} or parts.username or parts.password:
            raise RuntimeError("Ollama 仅允许访问本机回环地址，防止意外把本地接口当作远程代理")
        key = ""
    else:
        key = get_secret("llm_api_key")
        if not key:
            raise RuntimeError("已启用大模型但未配置 API Key；也可改用 Ollama 本地模型")
    _check_daily_limit(db)
    payload = {"model": settings["model"], "messages": messages, "temperature": 0.2,
               "max_tokens": 6000, "response_format": {"type": "json_object"}}
    if provider == "workbuddy_cloud":
        from .workbuddy_llm import call_workbuddy_full
        base = float(settings["timeout_seconds"])
        # Longer material legitimately takes longer; do not report a "stall" for a big article.
        timeout = min(1800.0, max(300.0, base + max(0, material_chars - 20000) / 2000 * 20))
        return await call_workbuddy_full(messages, settings["model"], timeout, 0.2,
                                         on_progress=_progress_sink(db, item_id) if item_id else None,
                                         stall_seconds=float(settings.get("stall_seconds", 300)))
    if provider == "ollama":
        import httpx
        timeout = max(600.0, float(settings["timeout_seconds"]))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            result = await client.post(endpoint, json=payload)
            result.raise_for_status()
            if len(result.content) > 20_000_000:
                raise ValueError("本地模型响应超过大小限制")
            response = result.json()
    else:
        response, _ = await post_json(endpoint, payload, headers={"Authorization": f"Bearer {key}"},
                                      timeout=float(settings["timeout_seconds"]), max_bytes=20_000_000)
    return {"content": response["choices"][0]["message"]["content"]}


def _parse_json(content: str) -> dict[str, Any]:
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def _insufficient(db: Database, item_id: str, reason: str) -> None:
    analysis = _normalise_analysis({
        "analysis_mode": "insufficient_source",
        "summary": f"无法详细归档：{reason}",
        "hook": "无法详细归档：没有拿到足够原文",
        "plain_problem": f"没有取得足以讲清这篇研究的材料。{reason}",
        "answer": "为避免用只言片语编造解释，本条不做猜测性精读。可稍后重试，或手动补充原文/PDF 后再精读。",
        "limits": ["本条未做精读，不代表研究质量判断。"],
        "evidence_gaps": [reason],
        "confidence_note": "insufficient",
        "relevance_score": 0, "importance_score": 0, "category": "待补充原文",
    })
    analysis["prompt_version"] = get_settings()["analysis"]["prompt_version"]
    db.save_analysis(item_id, analysis, "insufficient")


def _is_picture_led(item: dict[str, Any]) -> bool:
    """True when a post is carried by figures or a video instead of prose.

    Nothing is missing here: a two-panel anatomy plate or a fifteen-minute video
    article has no more text to read out.  A paper-grade reading of it would only
    invent a structure that the author never wrote.
    """
    metadata = json_loads(item.get("metadata_json"), {}) or {}
    body = (item.get("text") or "").strip()
    if metadata.get("media_kind") == "video":
        return True
    if metadata.get("archive_completeness") == "images_only":
        return True
    if metadata.get("archive_completeness") == "share_card":
        # 图片消息：微信只给验证页，能留下的就是分享卡片那段说明。
        # 这不是「没抓到材料」，是内容形态本身如此，按原样记录即可。
        return True
    if len(body) >= 1200:
        return False
    if extract_dois(body):
        # A DOI means a paper stands behind the post; that earns a real reading.
        return False
    return len(metadata.get("images") or []) >= 2 or len(body) >= 200


def _record_only(db: Database, item_id: str, body: str, images: int) -> None:
    """Log a short illustrated post as itself instead of calling it unarchivable.

    A two-panel anatomy plate or a single parameter table has little prose by
    design.  Running a paper-grade reading on it yields nothing, and labelling it
    「无法详细归档」 is simply wrong — nothing failed, the post is just short.
    """
    settings = get_settings()
    item = db.one("SELECT * FROM items WHERE id=?", (item_id,)) or {}
    existing = json_loads(item.get("analysis_json"), {}) or {}
    if existing.get("analysis_mode") == "llm" and (existing.get("summary") or "").strip():
        # A real reading already exists; a lighter pass must never overwrite it.
        log.info("record_only skipped for %s: a full reading is already stored", item_id)
        return
    local = _rules_analysis(item, settings)
    metadata = json_loads(item.get("metadata_json"), {}) or {}
    shared_by_user = metadata.get("share_channel") == "wechat_self_chat"
    lead = head_tail((body or "").strip().replace("\n", " "), 220)
    share_card = metadata.get("archive_completeness") == "share_card"
    shape = "图片消息" if share_card else ("图片为主" if images >= 2 else "短正文")
    analysis = dict(local)
    if share_card:
        analysis.update({
            "analysis_mode": "record_only",
            "summary": (f"图片消息（微信只给验证页，留下卡片上的 {len(body)} 字说明）：{lead}"
                        if lead else "图片消息：微信只给验证页，卡片上也没有说明文字。"),
            "one_sentence": head_tail(item.get("title") or "", 120),
            "limits": ["这条是公众号「图片消息」，正文是图片，微信对这类页面只返回人机验证页。"
                       "本地保存的是分享卡片上的说明文字，不是原文；要看原图请点原始链接。"],
            "evidence_gaps": ["没有拿到正文和原图；卡片说明由发布者自己写，可能比正文简略。"],
            "content_shape": shape,
            "confidence_note": "share_card_only",
        })
        analysis["prompt_version"] = settings["analysis"]["prompt_version"]
        score = float(analysis.get("relevance_score") or 0)
        analysis["relevance_score"] = min(max(score, 46.0), 60.0)
        threshold = float(settings["notifications"]["min_relevance"])
        db.save_analysis(item_id, analysis, "ready" if analysis["relevance_score"] >= threshold else "filtered")
        return
    analysis.update({
        "analysis_mode": "record_only",
        "summary": (f"图文短记（正文 {len(body)} 字、配图 {images} 张）：{lead}" if lead
                    else f"图文短记：原文没有正文，内容就是 {images} 张配图。"),
        "one_sentence": head_tail(item.get("title") or "", 120),
        "limits": [f"本条是图文短记：正文 {len(body)} 字、配图 {images} 张，信息主要在图片和正文本身。"
                   f"没有被引出的论文可读，因此不做论文级精读——这是内容形态，不是归档失败。"],
        "evidence_gaps": ["原文没有展开论述；若要深挖可看配图或表格里的细节。"],
        "content_shape": shape,
        "confidence_note": "article_only",
    })
    if shared_by_user or metadata.get("admission_filter"):
        # The item already passed admission (a tracked account, or a hand-forwarded
        # post).  A short illustrated piece must not be pushed back out by a lexical
        # score that has almost no text to score.  Cap it for the same reason: a term
        # like "DNA" saturates the lexical score, and a short record should never
        # outrank a real paper in the digest or trip the instant-push threshold.
        score = float(analysis.get("relevance_score") or 0)
        analysis["relevance_score"] = min(max(score, 46.0), 60.0)
    analysis["prompt_version"] = settings["analysis"]["prompt_version"]
    threshold = float(settings["notifications"]["min_relevance"])
    status = "ready" if float(analysis.get("relevance_score") or 0) >= threshold else "filtered"
    db.save_analysis(item_id, analysis, status)
    if status == "ready":
        from .notify import queue_for_notification
        queue_for_notification(db, item_id)


async def analyze_item(db: Database, item_id: str, force: bool = False) -> None:
    item = db.one("SELECT * FROM items WHERE id=?", (item_id,))
    if not item:
        raise ValueError("条目不存在")
    if item.get("excluded_at"):
        return
    settings = get_settings()
    local_analysis = _rules_analysis(item, settings)
    if not settings["llm"]["enabled"]:
        analysis = local_analysis
    elif not force and _is_picture_led(item):
        # 内容形态先于相关度判断：一篇 600 字的解剖图集本地关键词分可能是 0，
        # 但它并不是「没抓到材料」，只是本来就靠图和短文承载。
        # 这道判断必须排在 min_local_relevance 之前，否则永远走不到。
        metadata = json_loads(item.get("metadata_json"), {}) or {}
        _record_only(db, item_id, (item.get("text") or "").strip(),
                     len(metadata.get("images") or []))
        return
    elif not force and local_analysis["relevance_score"] < float(settings["llm"].get("min_local_relevance", 40)):
        analysis = dict(local_analysis)
        analysis.update({
            "summary": "本地初筛未达到云精读门槛；正文与证据已保留，可在详情页手动强制精读。",
            "analysis_mode": "local_prefilter", "llm_call_saved": True,
            "limits": ["仅完成本地规则初筛，未消耗云模型点数；如需语义分析请手动强制精读。"],
            "evidence_gaps": ["尚未调用大模型；本地规则可能漏判，手动精读可绕过此门槛"],
        })
        analysis["prompt_version"] = settings["analysis"]["prompt_version"]
        db.save_analysis(item_id, analysis, "filtered")
        return
    else:
        paper_text, papers = _paper_material(db, item_id)
        body = (item.get("text") or "").strip()
        # Decide which archived images deserve disk space before they enter the prompt.
        await triage_archived_images(db, item_id)
        item = db.one("SELECT * FROM items WHERE id=?", (item_id,)) or item
        metadata = json_loads(item.get("metadata_json"), {}) or {}
        cards = figure_cards(item)
        has_paper_text = bool(paper_text) and "未取得全文" not in paper_text
        image_count = len(metadata.get("images") or [])
        # A one-line WeChat brief is not a source: refuse to invent a mechanism from it.
        detailed = len(body) >= 1200 or len([c for c in cards if c.get("caption")]) >= 3
        if not detailed and not has_paper_text:
            if image_count >= 2 or len(body) >= 200 or metadata.get("media_kind") == "video":
                # 图集、表格或一篇几百字的科普短文：内容本身就这么长，
                # 没有论文可读，按原样记录即可，不该扣上「无法详细归档」。
                _record_only(db, item_id, body, image_count)
                return
            reason = ("公众号正文只有 " + str(len(body)) + " 字，且未取得论文全文（开放获取与网页均未拿到）"
                      if body else "没有抓到正文，也未取得论文全文")
            _insufficient(db, item_id, reason)
            return
        ideas = _relevant_ideas(db, item["title"] + "\n" + body, int(settings["llm"]["max_ideas"]))
        visible_text, selected = _visible_figure_text(cards, int(settings["llm"]["max_images"]))
        material = "【归档正文】\n" + (body or "（无正文）")
        if paper_text:
            material += "\n\n【关联论文材料】\n" + paper_text
        max_chars = int(settings["llm"]["max_input_chars"])
        if len(material) > max_chars and settings["llm"].get("long_material_strategy", "head_tail") == "head_tail":
            material = head_tail(material, max_chars)
        elif len(material) > max_chars:
            chunk_size = max(12000, max_chars // 2)
            chunks = [material[i:i + chunk_size] for i in range(0, len(material), chunk_size)][:8]
            notes = []
            for index, chunk in enumerate(chunks, 1):
                prompt = (f"这是长材料的第 {index}/{len(chunks)} 段。请提取研究问题、方法、关键数字与结果、局限、DOI和值得核查之处，"
                          "保留证据归属，不做最终综合。输出JSON，字段为 notes（字符串数组）和 evidence_gaps（字符串数组）。\n\n" + chunk)
                response = await _llm_call(db, [{"role": "system", "content": SYSTEM_PROMPT},
                                                {"role": "user", "content": prompt}], item_id)
                notes.append(_parse_json(response["content"]))
            material = "【分段证据笔记；完整原文已归档】\n" + json.dumps(notes, ensure_ascii=False)
        idea_text = json.dumps([{"id": x["id"], "title": x["title"], "content": head_tail(x["content"], 5000),
                                 "tags": json_loads(x["tags_json"], [])} for x in ideas], ensure_ascii=False)
        completeness = metadata.get("archive_completeness", "unknown")
        custom = settings["analysis"].get("custom_reading_prompt") or "无额外自定义标准"
        prompt = f"""【来源】{item['url']}
【标题】{item['title']}
【证据完整度】{completeness}（full_text_and_images=全文和图片；metadata_and_description_only=只有标题简介）
【可引用图片】{visible_text}
【归档元数据】{json.dumps({k: v for k, v in metadata.items() if k != 'images'}, ensure_ascii=False)[:6000]}
【用户既往精读补充标准】{custom}
【用户已有 ideas；禁止捏造其他 idea】{idea_text}
【待读材料】{material}

请按 system 里的字段要求返回最终精读 JSON。写作顺序必须是：这是什么问题 → 为什么难 → 他们怎么想到的 → 每一步做了什么看到什么 → 所以结论是什么。limits 和 next_questions 放在最后，不要混进前面。"""
        user_content: Any = prompt
        if settings["llm"]["vision_enabled"]:
            image_parts = _image_content(cards, selected, int(settings["llm"]["max_images"]))
            if image_parts:
                user_content = [{"type": "text", "text": prompt + "\n另附原文图片，编号已标注；只能引用这里出现过的图号。"}] + image_parts
        try:
            response = await _llm_call(db, [{"role": "system", "content": SYSTEM_PROMPT},
                                            {"role": "user", "content": user_content}],
                                       item_id, len(material))
        except RuntimeError as exc:
            # Some payloads are rejected as invalid; retry once without images before giving up.
            if not isinstance(user_content, list) or "request_invalid_parameter" not in str(exc):
                raise
            from .workbuddy_llm import payload_summary
            text_only = [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": prompt + "\n（图片无法随请求上传，请仅依据文字与元数据精读；不要引用任何图号）"}]
            try:
                response = await _llm_call(db, text_only, item_id, len(material))
            except RuntimeError as second:
                raise RuntimeError(f"{second}（已跳过图片重试仍失败；原请求：{payload_summary({'messages': [{'role': 'user', 'content': user_content}]})}）") from None
        analysis = _normalise_analysis(_parse_json(response["content"]))
        usage = response.get("usage") or {}
        analysis.update({"analysis_mode": "llm", "model": settings["llm"]["model"],
                         "evidence_completeness": completeness, "ideas_considered": [x["id"] for x in ideas],
                         "papers_considered": [{"doi": p["doi"], "status": p["status"]} for p in papers],
                         "llm_meta": {"elapsed_ms": response.get("elapsedMs"),
                                      "reasoning_chars": (response.get("progress") or {}).get("reasoningChars"),
                                      "content_chars": (response.get("progress") or {}).get("contentChars"),
                                      "completion_tokens": usage.get("completion_tokens"),
                                      "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}})
    analysis["prompt_version"] = settings["analysis"]["prompt_version"]
    status = "ready" if analysis["relevance_score"] >= float(settings["notifications"]["min_relevance"]) else "filtered"
    db.save_analysis(item_id, analysis, status)
    if status == "ready":
        from .notify import queue_for_notification
        queue_for_notification(db, item_id)
