"""Fast, local-only admission rules for the research library.

The main library intentionally contains only WeChat official-account articles and
Bilibili videos. No cloud model is called at this stage.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..settings import get_settings
from ..utils import canonical_url, json_loads

# Broad natural-science and research vocabulary. Matching is deliberately done on
# the title/description before ingestion; archived text is only used for cleanup.
DOMAIN_TERMS = {
    "科研", "科学", "科学家", "科普", "科学传播", "研究", "论文", "文献", "综述", "实验", "试验", "机制", "发现",
    "学术", "期刊", "预印本", "同行评议", "国家自然科学基金", "诺奖", "诺贝尔",
    "生命科学", "生物学", "生物医学", "医学", "临床", "基础医学", "药学", "药理", "药物", "疗法", "治疗",
    "细胞", "干细胞", "线粒体", "染色体", "核糖体", "基因", "基因组", "蛋白", "蛋白质", "酶", "代谢", "免疫", "炎症", "肿瘤", "癌症",
    "阿尔茨海默", "帕金森", "糖尿病", "心血管疾病", "高血压", "抑郁症", "药物研发", "临床试验",
    "神经", "脑科学", "大脑", "神经元", "认知", "微生物", "病毒", "细菌", "真菌", "病原", "疫苗", "抗体",
    "遗传", "进化", "生态", "动物", "植物", "海洋科学", "古生物", "生殖", "发育", "衰老", "再生",
    "分子", "原子", "化学", "催化", "氧化", "还原反应", "有机化学", "无机化学", "元素周期", "材料", "纳米", "高分子", "晶体", "超导", "电池",
    "物理", "量子", "粒子", "相对论", "热力学", "光学", "激光", "声学", "力学", "核聚变", "核物理",
    "数学", "统计学", "几何", "拓扑", "代数", "数论", "计算数学", "定理", "猜想", "方程", "概率论", "图论", "密码学",
    "天文", "宇宙", "星系", "黑洞", "恒星", "行星", "太阳系", "暗物质", "暗能量", "引力波", "射电望远镜",
    "地球科学", "地质", "地震", "火山", "气候科学", "大气科学", "环境科学", "海洋学",
    "人工智能", "机器学习", "深度学习", "神经网络", "大模型", "语言模型", "计算机科学", "算法", "机器人",
    "芯片", "半导体", "量子计算", "生物信息", "计算生物", "结构生物", "合成生物", "基因编辑", "多组学",
    "单细胞", "空间转录组", "转录组", "蛋白质组", "代谢组", "表观遗传", "crispr", "alphafold", "pymol",
    "冷冻电镜", "电镜", "显微镜", "质谱", "光谱", "色谱", "测序", "pcr", "western blot", "流式细胞",
    # 补一批高频具体名词：只靠"研究/发现"这类泛词判断时，很多真论文会被误判成不相关。
    "血小板", "肝癌", "肺癌", "胃癌", "乳腺癌", "白血病", "淋巴瘤", "转移", "耐药", "靶点", "受体", "配体",
    "底物", "酶活", "水凝胶", "皮肤", "伤口愈合", "原虫", "寄生虫", "真菌", "菌群", "菌株", "菌落",
    "小鼠", "大鼠", "动物模型", "细胞系", "活检", "血清", "血浆", "队列", "随访", "临床研究", "适应症",
    "染料", "荧光", "探针", "标记", "电泳", "免疫组化", "染色", "切片", "转染", "敲除", "过表达",
    "电化学", "电极", "电解", "储能", "光催化", "催化剂", "配位", "络合", "螯合", "合金", "陶瓷", "纤维",
    "涂层", "薄膜", "二维材料", "石墨烯", "钙钛矿", "自旋", "磁性", "光子", "等离激元", "晶圆", "集成电路",
    "结构解析", "动力学", "热力学", "相变", "缺陷", "界面", "纳米材料", "分子筛", "高分子", "聚合物",
    "基因组学", "蛋白质组", "代谢组", "微生物组", "转录调控", "信号通路", "细胞周期", "细胞死亡", "凋亡",
    "神经科学", "突触", "环路", "行为学", "认知神经", "脑机接口", "类脑", "影像学", "核磁", "超声",
    "流行病学", "统计学", "随机对照", "meta分析", "系统评价", "生物标志物", "精准医疗", "基因治疗", "细胞治疗",
    "nature", "science", "cell", "lancet", "nejm", "jama", "pnas", "doi", "arxiv", "biorxiv", "medrxiv",
    "pubmed", "crossref", "openalex", "unpaywall", "preprint", "journal", "research", "study", "abstract",
    "methods", "results", "experiment", "clinical", "protein", "genome", "gene", "rna", "dna", "mechanism",
    "review", "trial", "therapy", "neuron", "molecular", "quantum", "astrophys", "biomedical", "bioinformatics",
}

# These are admission exclusions, not merely notification downgrades. A research
# account can still publish advertisements, recruiting notices or campus logistics.
EXCLUDE_TERMS = {
    "广告", "推广", "商务合作", "课程报名", "报名链接", "付费课程", "培训营", "研修班", "开课", "学习资料", "招生简章", "招生通知",
    "招聘", "求职", "实习招聘", "科研助理招聘", "带货", "促销", "优惠券", "团购", "拼团", "抽奖", "直播带货",
    "美食", "探店", "旅游攻略", "酒店", "穿搭", "美妆", "明星", "八卦", "综艺", "电视剧", "电影解说",
    "游戏攻略", "游戏下载", "电竞", "番剧", "小说", "网文", "表情包", "壁纸", "头像", "情感咨询",
    "放假通知", "军训", "课表", "成绩查询", "考试报名", "四六级", "考研报名", "校园卡", "宿舍", "食堂",
    # 招生 / 课程 / 会议报名：这些是公众号里最常见的"非科研内容"，但正文全是专业词，
    # 光看关键词一定会放进来，只能靠标题里的这些词挡。
    "招生", "招收", "宣讲", "夏令营", "培训班", "训练营", "研修", "报名", "扫码", "学员", "学费", "开班",
    "课程", "上课", "第1课", "第2课", "第3课", "第 1 课", "第 2 课", "第 3 课",
    "考研", "保研", "推免", "复试", "调剂", "真题", "试题", "复习", "分数线",
    "羽球", "正手", "反手", "握拍", "发球", "球技", "夺冠", "联赛", "赛程", "直播预约",
    "年度会员", "免费领", "问卷",
}

# 社会新闻 / 司法案件 / 人事处分：科研类公众号常在同一账号里顺手发这类消息，
# 它们和科研无关，也不该走"低相关"占着资料库的位置。只看标题和简介，够精确。
# 刻意不收的词：自杀（细菌"自杀毒素"）、逮捕（诺奖得主被捕）、道歉/致歉（常出现在
# 无关文章的摘要里）、处分（会误伤"被给予开除处分"的科研诚信事件）。
SOCIAL_NEWS_TERMS = {
    "性骚扰", "猥亵", "强奸", "霸凌", "校园欺凌", "欺凌", "殴打", "打人",
    "实名举报", "舆情", "通报批评", "免职", "撤职", "停职",
    "落马", "双规", "留置", "立案调查", "接受调查", "判刑", "获刑", "有期徒刑", "刑拘",
    # 新闻摘要栏目：一篇里混着高校人事、案件、政策，读起来不是研究内容
    "一周动态", "一周要闻", "本周要闻", "新闻速览",
}

# "性侵"不能直接做子串匹配："胰腺癌恶性侵袭"里就藏着"性侵"。
SOCIAL_NEWS_PATTERNS = (re.compile(r"性侵(?!袭|入|略|蚀|润)"),)

# 这些词在任何公众号里都会出现，单靠它们说明不了"这是科研内容"。
WEAK_TERMS = {"科研", "科学", "科学家", "科学传播", "研究", "论文", "文献", "综述", "实验", "试验",
              "机制", "发现", "学术", "期刊", "预印本", "同行评议", "科普", "前沿", "成果", "团队",
              "技术", "方法", "分析", "数据", "系统", "报告", "进展", "专家", "学者",
              # 默认词表里的复合词，本质还是上面这些泛词
              "研究进展", "研究发现", "科学研究", "最新研究", "research", "study", "abstract",
              "methods", "results", "experiment", "clinical", "review", "journal", "scientific",
              "analysis", "研究团队"}

URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.I)
LATIN_TERM_RE = re.compile(r"^[a-z][a-z0-9 +.-]*$", re.I)

# ---------------------------------------------------------------------------
# 引流软文识别
#
# 有些账号发的是「借一篇技术文章把人引到自己的平台注册/下载」的软文：
# 正文看着全是专业词，其实重点是文末那句「扫码下载资料」「立即注册」。
# 这类不该占资料库的位置，但误伤真论文的代价更大，所以判据刻意收紧：
#   命中硬引流话术 **且** 正文里找不到任何学术证据（DOI / 参考文献 / 图表编号）。
# 真正的论文、综述、会议报道几乎不可能同时满足这两条。
MARKETING_STRONG = (
    "扫码下载", "扫码领取", "扫码注册", "扫码报名", "扫码观看", "扫码加入", "扫码进群",
    "扫码免费下载", "免费下载", "长按识别二维码", "点击下方名片", "关注后回复", "回复「", '回复"',
    "立即下载", "点击下载", "免费领取", "免费试用", "申请试用", "立即注册", "注册下载",
    "预约直播", "加入社群", "添加客服", "知识星球", "小鹅通", "限时免费", "限时优惠",
)
MARKETING_WEAK = (
    "扫码", "二维码", "下载资料", "下载pdf", "阅读原文", "关注公众号", "选型", "datasheet",
    "数据手册", "白皮书", "产品手册", "报名", "优惠", "购买", "下单", "客服",
)
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
FIGURE_RE = re.compile(r"(?:图|fig(?:ure)?\.?)\s*\d+")
# 正文前 40% 是「这篇想干什么」的位置；再往后基本是文末二维码和推荐阅读。
HEAD_RATIO = 0.4


def _academic_evidence(text: str) -> list[str]:
    folded = text.casefold()
    found: list[str] = []
    if DOI_RE.search(text):
        found.append("DOI")
    if "参考文献" in text or "references" in folded:
        found.append("参考文献")
    if FIGURE_RE.search(folded):
        found.append("图表编号")
    if "arxiv" in folded or "biorxiv" in folded or "medrxiv" in folded:
        found.append("预印本")
    if "et al" in folded:
        found.append("et al")
    return found


def detect_marketing(body: str) -> dict[str, Any]:
    """判断正文是不是以引流为目的的软文，返回 {blocked, score, reason, signals}。

    判据的关键是**位置**：真正讲研究的文章把「扫码关注」放在文末，正文开头
    讲的是研究本身；软文则一上来就告诉你「扫码下载资料」。所以只看前 40%。
    """
    text = (body or "")[:20000]
    length = len(text)
    if length < 120:
        return {"blocked": False, "score": 0, "reason": "", "signals": []}
    folded = text.casefold()
    evidence = _academic_evidence(text)
    if evidence:
        return {"blocked": False, "score": 0,
                "reason": f"正文有学术证据（{'、'.join(evidence)}）", "signals": evidence}

    strong: list[tuple[str, float]] = []
    for term in MARKETING_STRONG:
        index = folded.find(term.casefold())
        if index >= 0:
            strong.append((term, index / length))
    head_hits = [term for term, ratio in strong if ratio <= HEAD_RATIO]
    if head_hits:
        return {"blocked": True, "score": 2 + min(2, len(strong) - 1),
                "reason": f"正文开头就在引导扫码/下载（{'、'.join(head_hits[:4])}），"
                          f"没有 DOI、参考文献或图表编号",
                "signals": head_hits[:6]}

    weak = [term for term in MARKETING_WEAK if term.casefold() in folded]
    if len(weak) >= 4 and length < 3000:
        return {"blocked": True, "score": 1,
                "reason": f"正文是产品/活动介绍（{'、'.join(weak[:4])}），短且没有 DOI、参考文献或图表编号",
                "signals": weak[:6]}
    return {"blocked": False, "score": 0, "reason": "", "signals": weak[:6]}


def _metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    metadata = candidate.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return json_loads(candidate.get("metadata_json"), {}) or {}


def supported_library_url(kind: str, url: str) -> tuple[bool, str]:
    """Return whether the URL belongs to one of the two requested library types."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False, "链接格式无效"
    if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
        return False, "链接格式无效"
    if kind == "article":
        if host == "mp.weixin.qq.com" or host.endswith(".mp.weixin.qq.com"):
            return True, ""
        return False, "非公众号文章链接"
    if kind == "video":
        if host == "b23.tv":
            return True, ""
        if (host == "bilibili.com" or host.endswith(".bilibili.com")) and "/video/" in parts.path:
            return True, ""
        return False, "非B站视频链接"
    return False, "不是支持的资料类型"


def _contains_term(text: str, term: str) -> bool:
    term = term.casefold().strip()
    if not term:
        return False
    if LATIN_TERM_RE.match(term) and " " not in term and len(term) <= 8:
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None
    return term in text


def assess_candidate(candidate: dict[str, Any], include_archived_text: bool = False) -> dict[str, Any]:
    """Decide admission locally and return a non-sensitive explanation."""
    kind = str(candidate.get("kind") or "article")
    url = str(candidate.get("url") or "").strip()
    supported, reason = supported_library_url(kind, url)
    if not supported:
        return {"keep": False, "reason": reason, "matched_terms": []}

    metadata = _metadata(candidate)
    title = str(candidate.get("title") or "").strip()
    author = str(candidate.get("author") or "").strip()
    description = str(metadata.get("description") or metadata.get("description_preview") or "").strip()
    header = "\n".join(part for part in (title, description) if part and not URL_ONLY_RE.match(part))
    evidence_parts = [title, description]
    if include_archived_text:
        evidence_parts.extend([str(candidate.get("text") or "")[:30000], str(candidate.get("summary") or "")])
    evidence = "\n".join(part for part in evidence_parts if part and not URL_ONLY_RE.match(part))
    folded = evidence.casefold()

    settings = get_settings()
    exclusions = sorted(EXCLUDE_TERMS | {str(x).strip() for x in settings.get("exclude_keywords", [])})
    # Exclusion words must only be judged on the headline/intro. An article that
    # says "把这套思路推广到其他反应" in its body is research, not an advert.
    header_folded = header.casefold()
    excluded_hits = [term for term in exclusions if _contains_term(header_folded, term)]
    if excluded_hits:
        return {"keep": False, "reason": "广告、招聘、生活或校园事务等内容", "matched_terms": []}

    # 社会新闻与案件通报单独判一次，理由要写给用户看：这类不是"低相关"，是不该收。
    news_hits = [term for term in SOCIAL_NEWS_TERMS if _contains_term(header_folded, term)]
    news_hits += [found.group(0) for found in
                  (pattern.search(header_folded) for pattern in SOCIAL_NEWS_PATTERNS) if found]
    if news_hits:
        return {"keep": False, "reason": "社会新闻、案件处分或新闻摘要栏目，与科研阅读无关",
                "matched_terms": []}

    terms = sorted(DOMAIN_TERMS | {str(x).strip() for x in settings.get("science_keywords", [])})
    matched = [term for term in terms if _contains_term(folded, term)]
    specific = [term for term in matched if term.casefold() not in WEAK_TERMS]
    title_hits = [term for term in matched if _contains_term(title.casefold(), term)]

    # 用户明确启用的重点账号要先放行，而且必须排在泛词拦截**前面**。
    # 广告、引流、社会新闻上面已经挡过一轮；剩下的内容，用户把这个号勾进
    # 追踪名单本身就是"我要看"的表态。2026-11-19「民间审稿办主任」那条
    # "培养皿可能是你实验的最大因素" 标题只命中"实验"这个泛词，在这里被
    # 静默丢弃，连 filtered 记录都没留，用户搜不到也查不出。
    if metadata.get("catalog_matched") and title and not URL_ONLY_RE.match(title):
        return {"keep": True, "reason": "来自公众号库中已启用的重点账号", "matched_terms": matched[:12]}

    # 只命中"研究""发现"这类泛词不算证据：真正的研究报道，标题里一定有具体名词
    # （蛋白、纳米、量子…）。这一条挡掉的是"发现好多大学生不懂邮件礼仪"这种。
    if specific or len(title_hits) >= 2:
        return {"keep": True, "reason": "标题或简介命中科研主题", "matched_terms": matched[:12]}
    if matched:
        return {"keep": False, "reason": "只命中了研究/发现这类泛词，看不出具体科研主题",
                "matched_terms": matched[:6]}

    # An explicit manual submission is an intentional override of the automatic
    # title filter, but still restricted to the two supported content types.
    if metadata.get("manual_ingest"):
        return {"keep": True, "reason": "用户手动提交", "matched_terms": []}

    if not evidence.strip():
        return {"keep": False, "reason": "只有裸链接，无法确认科研相关性", "matched_terms": []}
    return {"keep": False, "reason": "标题和简介未显示自然科学或科研相关性", "matched_terms": []}


def cleanup_existing_items(db: Any) -> dict[str, int]:
    """Reversible startup sweep; also catches rows written by an older worker."""
    # If the local vocabulary is expanded, reconsider prior automatic
    # "not enough evidence" exclusions. Manual/duplicate/broken-link actions stay.
    reconsider_reasons = {"标题和简介未显示自然科学或科研相关性", "只有裸链接，无法确认科研相关性",
                          "广告、招聘、生活或校园事务等内容",
                          "只命中了研究/发现这类泛词，看不出具体科研主题"}
    restore_ids: list[str] = []
    for row in db.query("SELECT * FROM items WHERE excluded_at IS NOT NULL"):
        if row.get("exclusion_reason") in reconsider_reasons and assess_candidate(row, include_archived_text=False)["keep"]:
            restore_ids.append(row["id"])
    restored = db.set_items_excluded(restore_ids, "科研词表扩充后复核", excluded=False) if restore_ids else 0
    groups: dict[str, list[str]] = {}
    visible = 0
    for row in db.query("SELECT * FROM items WHERE excluded_at IS NULL"):
        visible += 1
        # 和入库时用同一套证据（标题 + 简介）。以前这里看全文，导致"正文里提了一句
        # 蛋白"的招生贴、读博故事永远清不掉。
        assessment = assess_candidate(row, include_archived_text=False)
        if not assessment["keep"]:
            groups.setdefault(assessment["reason"], []).append(row["id"])
    changed = 0
    for reason, item_ids in groups.items():
        changed += db.set_items_excluded(item_ids, reason)
    return {"checked": visible, "restored": restored, "excluded": changed, "remaining": visible - changed}


def cleanup_marketing_items(db: Any, limit: int = 5000) -> int:
    """把已经归档、但正文其实是引流软文的条目移出资料库。

    只在正文有学术证据时才放行，判据见 detect_marketing；误判的可以从
    「已过滤」里一键恢复，所以这里不必追求零误伤，但不能放过明显的软文。
    """
    rows = db.query(f"""SELECT id,title,text FROM items
        WHERE excluded_at IS NULL AND kind='article' AND length(coalesce(text,''))>=120
        ORDER BY discovered_at DESC LIMIT {int(limit)}""")
    blocked: list[tuple[str, str]] = []
    for row in rows:
        verdict = detect_marketing(row.get("text") or "")
        if verdict["blocked"]:
            blocked.append((str(row["id"]), verdict["reason"]))
    for item_id, reason in blocked:
        db.set_items_excluded([item_id], f"引流/营销文章：{reason}")
    if blocked:
        db.add_event("marketing_filter", f"正文复核：{len(blocked)} 条引流软文移出资料库")
    return len(blocked)


def dedupe_duplicate_items(db: Any) -> int:
    """Hide repeated copies of one article (same __biz+mid+idx, different share link)."""
    rows = db.query("""SELECT id,kind,url,status,html_path,discovered_at,excluded_at FROM items
        WHERE excluded_at IS NULL ORDER BY discovered_at""")
    rank = {"ready": 6, "archived": 5, "insufficient": 4, "filtered": 3, "error": 2, "discovered": 1}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["kind"]), canonical_url(str(row["url"] or "")))
        groups.setdefault(key, []).append(row)
    duplicates: list[str] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (rank.get(str(r["status"]), 0), bool(r.get("html_path")),
                                    str(r["discovered_at"] or "")), reverse=True)
        duplicates.extend(str(r["id"]) for r in members[1:])
    if duplicates:
        db.set_items_excluded(duplicates, "重复链接（同一篇文章的不同分享参数）")
    return len(duplicates)
