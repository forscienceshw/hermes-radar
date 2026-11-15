"""Web + literature search for the in-place assistant.

Two kinds of sources, on purpose:

* **文献源**（Europe PMC、arXiv）是结构化 API，不需要密钥，命中率高，返回摘要，
  对"这个蛋白还有什么别的功能""有没有更新的工作"这类问题比通用搜索更可靠。
* **通用网页**（Bing HTML）用来兜底新闻、会议、产品、百科这类文献库里没有的信息。

所有请求都走 `app.net.request`，因此仍然受公网地址校验（禁止 127.0.0.1、内网段）
和大小限制保护。任一源失败不影响其他源，失败原因会带回去给前端显示。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

from bs4 import BeautifulSoup

from ..net import request

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SNIPPET_CHARS = 300
# 拉丁字母技术词（蛋白名、基因名、方法名），用来拼第二条更"学术"的查询。
LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
STOP_TOKENS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "have",
    "has", "not", "but", "you", "can", "how", "why", "what", "which", "does", "did",
    "into", "than", "then", "also", "such", "more", "most", "does", "use", "used",
}

# 文献库是英文的，中文问题直接丢进去只会匹配到零散汉字。这里放一小张常用词表，
# 覆盖这个雷达里出现频率最高的方向；命中就拼进第二条查询，没命中就只走网页源。
GLOSSARY = {
    "纳米抗体": "nanobody", "单域抗体": "single-domain antibody", "亲和标签": "affinity tag",
    "泛素连接酶": "ubiquitin ligase", "泛素化": "ubiquitin conjugation", "泛素": "ubiquitin",
    "偶联化学": "conjugation chemistry", "化学合成": "chemical synthesis", "化学选择性": "chemoselectivity",
    "非天然氨基酸": "unnatural amino acid", "蛋白质设计": "protein design", "定向进化": "directed evolution",
    "冷冻电镜": "cryo-EM", "晶体结构": "crystal structure", "结构生物学": "structural biology",
    "单细胞": "single-cell", "转录组": "transcriptome", "基因编辑": "gene editing",
    "碱基编辑": "base editing", "转录因子": "transcription factor", "表观遗传": "epigenetics",
    "信使RNA": "mRNA", "核糖体": "ribosome", "蛋白酶": "protease", "激酶": "kinase",
    "抑制剂": "inhibitor", "抗体药物偶联": "antibody-drug conjugate", "免疫治疗": "immunotherapy",
    "肿瘤微环境": "tumor microenvironment", "类器官": "organoid", "细胞凋亡": "apoptosis",
    "自噬": "autophagy", "炎症小体": "inflammasome", "肠道菌群": "gut microbiota",
    "CRISPR": "CRISPR", "AlphaFold": "AlphaFold", "DNA支架": "DNA scaffold",
    "核酸纳米": "nucleic acid nanostructure", "适配体": "aptamer", "生物传感器": "biosensor",
    "酶催化": "enzymatic catalysis", "不对称合成": "asymmetric synthesis", "点击化学": "click chemistry",
    "局部拥挤": " macromolecular crowding", "相分离": "phase separation", "液液相分离": "LLPS",
    "神经退行": "neurodegeneration", "阿尔茨海默": "Alzheimer", "帕金森": "Parkinson",
    "胚胎发育": "embryonic development", "细胞核": "nucleus", "染色质": "chromatin",
    "有丝分裂": "mitosis", "干细胞": "stem cell", "重编程": "reprogramming",
}
CHINESE_QUESTION_WORDS = ("帮我搜一下", "帮我查一下", "帮我找一下", "帮我搜", "帮我查", "帮我找",
                          "搜一下", "查一下", "找一下", "我想知道", "麻烦", "请问", "给我",
                          "为什么", "怎么回事", "是什么", "有哪些", "怎么样", "如何", "是否",
                          "能不能", "可不可以", "有没有", "还有", "具体", "到底", "用了",
                          "哪种", "哪个", "哪些", "什么", "怎么", "可以", "告诉", "这个",
                          "那个", "一下", "以及")


def _clean(text: Any, limit: int = SNIPPET_CHARS) -> str:
    value = BeautifulSoup(str(text or ""), "lxml").get_text(" ")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


BING_ENDPOINT = "https://www.bing.com/search"


def _unwrap_bing(url: str) -> str:
    """Bing 有些结果仍是 /ck/a?u=a1<base64> 的跳转壳，还原成真实地址。"""
    if "bing.com/ck/a" not in url:
        return url
    token = (parse_qs(urlsplit(url).query).get("u") or [""])[0]
    if not token.startswith("a1"):
        return url
    payload = token[2:]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 解码失败就退回原地址
        return url
    return decoded if decoded.startswith("http") else url


def _parse_bing_rss(text: str, limit: int) -> list[dict[str, Any]]:
    """Bing 的 RSS 出口是纯 XML：不用解析整页脚本，也不会被结果页的重定向拖慢。"""
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except (ET.ParseError, ValueError):
        return []
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        title = _clean(item.findtext("title") or "", 160)
        link = _unwrap_bing((item.findtext("link") or "").strip())
        if not title or not link.startswith("http") or link in seen:
            continue
        seen.add(link)
        hits.append({
            "title": title,
            "url": link,
            "snippet": _clean(item.findtext("description") or ""),
            "source": f"网页 · {_host(link)}",
            "published": _clean(item.findtext("pubDate") or "", 40),
        })
        if len(hits) >= limit:
            break
    return hits


async def bing(query: str, limit: int = 4) -> list[dict[str, Any]]:
    params = urlencode({"q": query, "format": "rss", "count": str(max(limit * 3, 10))})
    response = await request(f"{BING_ENDPOINT}?{params}", headers=BROWSER_HEADERS,
                             timeout=15, max_bytes=2_000_000)
    return _parse_bing_rss(response.text, limit)


async def europe_pmc(query: str, limit: int = 4) -> list[dict[str, Any]]:
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           f"query={quote_plus(query)}&format=json&pageSize={max(limit, 4)}&resultType=core")
    response = await request(url, timeout=15, headers={"Accept": "application/json"},
                             max_bytes=12_000_000)
    payload = json.loads(response.text)
    hits: list[dict[str, Any]] = []
    for row in (payload.get("resultList") or {}).get("result") or []:
        doi = row.get("doi") or ""
        link = (f"https://doi.org/{doi}" if doi else
                (f"https://europepmc.org/article/{row.get('source') or 'MED'}/{row.get('id')}"
                 if row.get("id") else ""))
        if not link:
            continue
        journal = ((row.get("journalInfo") or {}).get("journal") or {}).get("title") or ""
        year = row.get("pubYear") or ""
        abstract = _clean(row.get("abstractText") or row.get("title"), SNIPPET_CHARS)
        hits.append({
            "title": _clean(row.get("title"), 180),
            "url": link,
            "snippet": abstract,
            "source": " · ".join(x for x in ("Europe PMC", journal, year) if x),
            "published": year,
        })
        if len(hits) >= limit:
            break
    return hits


async def arxiv(query: str, limit: int = 3) -> list[dict[str, Any]]:
    search = quote_plus(query)
    url = (f"https://export.arxiv.org/api/query?search_query=all:{search}"
           f"&start=0&max_results={max(limit, 3)}&sortBy=relevance")
    response = await request(url, timeout=15, max_bytes=4_000_000)
    soup = BeautifulSoup(response.text, "xml")
    hits: list[dict[str, Any]] = []
    for entry in soup.find_all("entry"):
        link = entry.find("id")
        title = entry.find("title")
        if not link or not title:
            continue
        published = (entry.find("published").get_text(strip=True)[:10]
                     if entry.find("published") else "")
        hits.append({
            "title": _clean(title.get_text(" "), 180),
            "url": link.get_text(strip=True),
            "snippet": _clean(entry.find("summary").get_text(" ") if entry.find("summary") else "",
                              SNIPPET_CHARS),
            "source": f"arXiv 预印本 · {published}",
            "published": published,
        })
        if len(hits) >= limit:
            break
    return hits


def _latin_terms(blob: str, limit: int = 6) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in LATIN_TOKEN.findall(blob):
        lowered = token.lower()
        if lowered in STOP_TOKENS or lowered in seen:
            continue
        seen.add(lowered)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens


def _translated_terms(blob: str, limit: int = 5) -> list[str]:
    return [en.strip() for zh, en in GLOSSARY.items() if zh.lower() in blob.lower()][:limit]


def _join_words(phrases: list[str], limit: int = 6) -> str:
    """把若干英文短语拼成一条查询，顺手去掉重复词（"conjugation" 只留一次）。"""
    words: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        for word in phrase.split():
            lowered = word.lower()
            # "AlphaFold" 和 "AlphaFold3" 只留一个，否则文献查询里同一个词出现两次。
            if lowered in seen or any(w.startswith(lowered) or lowered.startswith(w) for w in seen):
                continue
            seen.add(lowered)
            words.append(word)
    return " ".join(words[:limit])


def _web_query(question: str) -> str:
    """网页搜索喜欢短查询：去掉指令词、问句词和标点，压到 40 字以内。"""
    text = re.sub(r"[？?！!。，,；;：:\n\r\t「」“”\"']+", " ", question or "")
    for word in CHINESE_QUESTION_WORDS:
        text = text.replace(word, " ")
    text = re.sub(r"\s+", " ", text).strip()
    # 只剥开头残留的单字虚词（"是 偶联化学" → "偶联化学"）。单字不能全局替换，
    # 否则"有机化学"会被改成"机化学"。
    text = re.sub(r"^(?:是|有|和|跟|与|那|就|还|会|能)\s*", "", text)
    return text[:40].strip()


# 追问的典型说法：主语被省略，只有这些词。不补主语的话，搜回来的是同名不同事的内容。
FOLLOWUP_WORDS = ("帮我搜", "帮我查", "帮我找", "搜一下", "查一下", "找一下", "搜搜", "查查",
                  "它用", "它做", "它到底", "这个", "该方", "上面", "刚才", "其中", "这种")


def _looks_like_followup(question: str) -> bool:
    text = (question or "").strip()
    if any(w in text for w in FOLLOWUP_WORDS):
        return True
    # 很短、又没有英文技术词，多半是接着上一轮问的"它/那这个"。
    return len(text) <= 10 and not LATIN_TOKEN.search(text)


def _topic_terms(item_terms: list[str], limit: int = 2) -> list[str]:
    """给追问补主语：用精读结果里抽出来的名词。

    这些是干净的专业术语（"MESNa活化泛素"、"酰基-酶中间体"），比在标题上硬切
    中文靠谱得多——没有分词器时，标题切出来多半是"是干什么的"这种废词。
    """
    picked: list[str] = []
    for term in item_terms:
        value = str(term or "").strip()
        if len(value) < 2 or value.lower() in {p.lower() for p in picked}:
            continue
        picked.append(value)
        if len(picked) >= limit:
            break
    return picked


def plan_queries(question: str, item_title: str = "",
                 item_terms: list[str] | None = None) -> dict[str, Any]:
    """拆成两路：文献查询（英文技术词）和网页查询（压短后的原问题）。

    中文长句直接丢进 Europe PMC 只会匹配到零散汉字，返回一堆不相干的中文期刊，
    所以文献源只接受含英文技术词的查询；纯中文问题走术语表翻译，翻不出来就只搜网页。
    """
    base = re.sub(r"\s+", " ", question or "").strip()[:120]
    if not base:
        return {"science": [], "web": ""}
    terms_all = [str(t) for t in (item_terms or []) if t]
    blob = " ".join([question, item_title, " ".join(terms_all)])
    science: list[str] = []
    # 中文句子进不了文献库：Europe PMC 会把"支架里的局部拥挤效应"拆成单个汉字来匹配。
    if _latin_terms(base, 2) and not CJK_RUN.search(base):
        science.append(base)
    # 只从"问题本身"取英文词，不从标题取：标题里的作者名、期刊名（David Baker、
    # bioRxiv）会把文献查询带偏，Europe PMC 里除了同一篇什么都查不到。
    # 术语表翻译只在追问时才借文章的术语；问全新话题（"CRISPR 脱靶检测"）不能带上
    # 当前文章的"泛素化"，否则整条文献查询都跑偏。
    followup = _looks_like_followup(question)
    latin = _latin_terms(base)
    # 追问时用文章最靠前的几个名词（"泛素化"→ubiquitin conjugation）确定主题，
    # 再拿问题本身的术语去细化；直接用整段标题翻译，第一条常常是"ubiquitin ligase"
    # 这种把范围收得过窄的搭配。
    translated = ((_translated_terms(" ".join(terms_all[:4])) + _translated_terms(base))
                  if followup else _translated_terms(base))
    ordered = (translated + latin) if CJK_RUN.search(base) else (latin + translated)
    # Europe PMC 是相关性打分而不是布尔与：词越多越糊。实测"ubiquitin conjugation"
    # 命中率 100%，加一个 chemistry 就退化成一堆 0 相关的 19F 探针论文。所以只要两个词。
    academic = _join_words(ordered, limit=2)
    pure_latin = not CJK_RUN.search(base)
    # 纯英文问题已经在上面整句进过一次了，别把同一条再追加一遍。单个英文术语
    # （nanobody、CRISPR）本身就是有效查询，不必强求两个词。
    if academic and not (pure_latin and academic.lower() == base.lower()):
        science.append(academic)
    # 网页查询：追问（"它到底用了哪种偶联化学"）没有主语，必须把文章主题词补进去，
    # 否则检索到的只是这几个字随便撞上的东西——上一版就是这样搜出 Heck 反应的。
    web = _web_query(question) or base
    if followup:
        topic = _topic_terms(terms_all)
        if topic:
            web = " ".join(dict.fromkeys(topic + [web]))[:80]
    return {"science": science[:2], "web": web}


SOURCE_WEIGHT = (("Europe PMC", 1.0), ("arXiv", 0.9))


def _terms_of(text: str) -> list[str]:
    words = [w.lower() for w in LATIN_TOKEN.findall(text) if w.lower() not in STOP_TOKENS]
    words += [w for w in CJK_RUN.findall(text) if len(w) >= 2]
    return list(dict.fromkeys(words))


def _overlap(hit: dict[str, Any], query: str) -> float:
    """这条结果到底有多贴合"发给这个源的查询"，而不是原问题。

    用原问题算相关性时，"DNA" 这种词会让一堆无关的 DNA 论文都得高分；
    改成对各自查询算覆盖率，再把低覆盖的条目直接丢掉。
    """
    terms = _terms_of(query)
    if not terms:
        return 0.0
    text = f"{hit['title']} {hit['snippet']}".lower()
    matched = sum(1 for term in terms if term in text)
    return matched / len(terms)


def _source_weight(hit: dict[str, Any]) -> float:
    for prefix, weight in SOURCE_WEIGHT:
        if hit["source"].startswith(prefix):
            return weight
    return 0.5


async def gather(question: str, *, item_title: str = "", limit: int = 8,
                 allow_web: bool = True, web_limit: int = 3,
                 item_terms: list[str] | None = None) -> dict[str, Any]:
    """并发跑所有源，去重、排序、截断，返回可直接喂给模型的结构。"""
    started = time.monotonic()
    plan = plan_queries(question, item_title, item_terms)
    if not plan["science"] and not plan["web"]:
        return {"queries": [], "results": [], "errors": [], "elapsed_ms": 0}

    tasks: list[tuple[str, str, str, Any]] = []
    for index, query in enumerate(plan["science"]):
        tasks.append(("science", query, f"Europe PMC:{query}", europe_pmc(query, 3)))
        tasks.append(("science", query, f"arXiv:{query}", arxiv(query, 2)))
    if allow_web and plan["web"]:
        tasks.append(("web", plan["web"], f"Bing:{plan['web']}", bing(plan["web"], 5)))

    settled = await asyncio.gather(*(coro for *_, coro in tasks), return_exceptions=True)
    errors: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {"science": [], "web": []}
    seen: set[str] = set()
    web_raw = 0
    for (bucket, query, label, _), outcome in zip(tasks, settled):
        if isinstance(outcome, Exception):
            errors.append(f"{label.split(':')[0]} 失败：{type(outcome).__name__}: {str(outcome)[:120]}")
            continue
        query_terms = _terms_of(query)
        terms_in_query = len(query_terms)
        head_term = query_terms[0] if query_terms else ""
        for hit in outcome:
            key = re.sub(r"^https?://(www\.)?", "", hit["url"]).rstrip("/").lower()
            title_key = re.sub(r"[\W_]+", "", hit["title"].lower())[:40]
            if key in seen or (title_key and title_key in seen):
                continue
            if bucket == "web":
                web_raw += 1
            overlap = _overlap(hit, query)
            # 文献源：至少命中一半查询词，而且第一条（主题词）必须在场。
            # 少了后半条，查"ubiquitin conjugation"会混进一篇讲"复共轭代数"的数学论文。
            text = f"{hit['title']} {hit['snippet']}".lower()
            if bucket == "science" and (overlap < 0.5 or (head_term and head_term not in text)):
                continue
            # 网页源放低一点门槛，但多词查询仍要求命中至少三分之一：
            # Bing 被判定成自动查询时会退化成"只按首词出结果"，那些词典页就是这么来的。
            if bucket == "web" and terms_in_query >= 2 and overlap < 0.34:
                continue
            seen.add(key)
            if title_key:
                seen.add(title_key)
            buckets[bucket].append({**hit, "_rank": overlap * _source_weight(hit)})

    if allow_web and plan["web"] and web_raw and not buckets["web"]:
        errors.append(f"Bing 返回的 {web_raw} 条结果与「{plan['web']}」不相干，已全部丢弃"
                      "（多半是被判定成自动查询，只按首个关键词出结果）")

    for bucket in buckets.values():
        bucket.sort(key=lambda h: h["_rank"], reverse=True)
    ordered = buckets["science"][:max(limit - web_limit, 0)]
    ordered += buckets["web"][:web_limit]
    results: list[dict[str, Any]] = []
    used = 0
    budget = 3200
    for hit in ordered:
        cost = len(hit["title"]) + len(hit["snippet"])
        if used + cost > budget and results:
            continue
        used += cost
        clean = {k: v for k, v in hit.items() if not k.startswith("_")}
        results.append({**clean, "n": len(results) + 1})
        if len(results) >= limit:
            break
    return {"queries": plan["science"] + ([plan["web"]] if plan["web"] else []),
            "results": results, "errors": errors[:4],
            "elapsed_ms": int((time.monotonic() - started) * 1000)}


def render_block(payload: dict[str, Any]) -> str:
    """把检索结果排成带序号的材料块，模型用 [1][2] 引用。"""
    results = payload.get("results") or []
    queries = [q for q in (payload.get("queries") or []) if q]
    if not results:
        # 检索跑过但没结果时也必须告诉模型"已经搜过了"，否则它又会说"我无法联网"。
        if not queries:
            return ""
        reason = "；".join(payload.get("errors") or [])
        return ("【联网检索结果】系统已在用户的机器上实时检索（检索词："
                + "、".join(queries[:3]) + "），"
                + (f"但结果被过滤掉了：{reason}。" if reason else "但没有找到可用结果。")
                + "请直接说明这次没检索到，不要说'我无法联网'。")
    header = "【联网检索结果】系统已经在用户的机器上完成实时检索，下面就是检索到的内容"
    if queries:
        header += f"（检索词：{'、'.join(queries[:3])}）"
    lines = [header + "。引用时在句末标注 [序号]；检索没覆盖的就直说没有查到。"]
    for hit in results:
        head = f"[{hit['n']}] {hit['title']}"
        meta = " · ".join(x for x in (hit.get("source"), hit.get("url")) if x)
        lines.append(f"{head}\n    {meta}\n    {hit['snippet']}")
    return "\n".join(lines)
