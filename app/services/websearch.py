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
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlsplit

from bs4 import BeautifulSoup

from ..net import request

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
# DDG 的风控不看 UA，看"这次请求像不像人点进来的"：只给 User-Agent 会被判成
# 脚本，一律回 202 + 人机验证页（页面 15KB，没有结果，很容易被当成"搜不到"）。
# 下面这几个头是 Chrome 从地址栏打开页面时必带的，补齐后实测连续 4 次都是 200。
DUCK_HEADERS = {
    **BROWSER_HEADERS,
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

SNIPPET_CHARS = 300
# 召回阶段的整体预算。Europe PMC 从国内出去有时 3 秒、有时 14 秒（国际链路抖动，
# 与 DNS 无关，实测解析只要 20ms）。6.5 秒时它经常被整条跳过，而它恰恰是学术质量
# 最高的源；放到 8 秒能覆盖大部分抖动。仍到点就用手上已有的，慢的记一条说明。
SEARCH_BUDGET = 8.0
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

# 缩写 ↔ 全称。同一个东西两种写法，搜索引擎只覆盖其中一种：
# 搜「ADC」回来的全是英文综述，而写「抗体药物偶联物」的中文页面一条都进不来。
# 这一层不调模型，纯查表，多一条查询只多一次并发请求（不进关键路径）。
ABBREVIATIONS = {
    "adc": ("antibody-drug conjugate", "抗体药物偶联物"),
    "vhh": ("nanobody", "single-domain antibody", "纳米抗体"),
    "nanobody": ("VHH", "single-domain antibody", "纳米抗体"),
    "car-t": ("chimeric antigen receptor", "嵌合抗原受体"),
    "protac": ("proteolysis targeting chimera", "蛋白降解靶向嵌合体"),
    "crispr": ("CRISPR-Cas9", "基因编辑"),
    "mrna": ("messenger RNA", "信使RNA"),
    "sirna": ("small interfering RNA", "小干扰RNA"),
    "llps": ("liquid-liquid phase separation", "液液相分离"),
    "cryo-em": ("cryo-electron microscopy", "冷冻电镜"),
    "aav": ("adeno-associated virus", "腺相关病毒"),
    "tme": ("tumor microenvironment", "肿瘤微环境"),
    "egfr": ("epidermal growth factor receptor", "表皮生长因子受体"),
    "pd-l1": ("programmed death-ligand 1", "程序性死亡配体1"),
}


def _expand_queries(base: str, science: list[str], limit: int = 2) -> list[str]:
    """从已知查询派生变体：缩写展开、全称收回、英文问题补一条中文。

    这三条都是「同一个意思换个说法」的具体形态，命中率提升来自覆盖面而不是
    排序技巧。规则很朴素，但比让模型改写省 1–2 秒，也不会把查询改跑偏。

    **顺序按语言互补排**：中文问题先给英文全称（补文献库），英文问题先给中文
    （补中文网页）。两种都塞进同一队列再截断，会出现「英文问题拿到两条英文
    变体、中文网页一条都没覆盖」的情况——那等于没扩展。
    """
    pool = f"{base} {' '.join(science)}".strip()
    low = pool.lower()
    english: list[str] = []
    chinese: list[str] = []

    def add(item: str) -> None:
        item = re.sub(r"\s+", " ", str(item or "")).strip()
        if not item or item.lower() in low:
            return
        bucket = chinese if CJK_RUN.search(item) else english
        if item not in bucket:
            bucket.append(item)

    def has(token: str) -> bool:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", low))

    for short, longs in ABBREVIATIONS.items():
        if has(short):
            for long in longs:
                add(long)

    for short, longs in ABBREVIATIONS.items():
        if has(short):
            continue
        if any(long.lower() in low for long in longs):
            add(short.upper() if len(short) <= 3 else short)

    # 英文问题补一条中文：中文网页里"纳米抗体 亲和标签"这种写法，英文词搜不到。
    if not CJK_RUN.search(base):
        zh = [key for key, value in GLOSSARY.items()
              if len(value) > 3 and value.lower() in low and CJK_RUN.search(key)]
        if zh:
            add(" ".join(dict.fromkeys(zh))[:60])

    ordered = (english + chinese) if CJK_RUN.search(base) else (chinese + english)
    cleaned: list[str] = []
    for item in ordered:
        if item.lower() != base.lower() and item not in cleaned:
            cleaned.append(item)
    return cleaned[:limit]


def _clean(text: Any, limit: int = SNIPPET_CHARS) -> str:
    value = BeautifulSoup(str(text or ""), "lxml").get_text(" ")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _contact_email() -> str:
    """Crossref 礼貌池要的联系方式。没填就返回空，功能照常，只是配额低。"""
    try:
        from ..settings import get_settings
        return str((get_settings().get("papers") or {}).get("email") or "").strip()
    except Exception:                                     # noqa: BLE001 - 设置读不到不该拦住检索
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


SO360_ENDPOINT = "https://www.so.com/s"
# 360 在结果里会塞自家聚合页：图片搜索（pic.360.com）、站内搜索（sou.360.cn）。
# 这些点进去是同一批结果的另一个视图，当成"资料"喂给模型只会浪费位置。
JUNK_HOSTS = {"pic.360.com", "image.so.com", "sou.360.cn", "www.so.com", "so.com"}
# 360 每条结果尾巴上挂一行「cdmd.cnki.com.cn 反馈」，纯粹是站点标注，不是摘要。
# 这行经常和摘要挤在同一个 <p> 里，所以必须从文本尾部剥掉，不能整段匹配。
SITE_FEEDBACK = re.compile(r"\s*[\w.-]+\.[a-z]{2,}(?::\d+)?\s*反\s*馈\s*$", re.I)
# 360 的风控是间歇性的：同一个出口连搜几次后改返回 ~6KB 的验证页，HTTP 仍 200。
# 正常结果页都在 100KB 以上。用体积加关键词双重判断，命中就熔断，别反复白等。
SO360_COOLDOWN = 300.0
SO360_COOLDOWN_UNTIL = 0.0
SO360_BLOCK_MARKERS = ("captcha", "verify", "安全检查", "人机")


def _so360_blocked(html: str) -> bool:
    text = html or ""
    if len(text) < 20_000:
        return True
    if "res-list" in text:
        return False
    low = text.lower()
    return any(marker in low for marker in SO360_BLOCK_MARKERS)


def _squash(text: Any, limit: int = SNIPPET_CHARS) -> str:
    """合并标签缝隙里的空格。

    360 把命中词包在 <em> 里，get_text(" ") 会把「纳米抗体」切成「纳米 抗体」，
    后面按词做重叠计算时这一半词就永远匹配不上。
    """
    value = BeautifulSoup(str(text or ""), "lxml").get_text("")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit] + ("…" if len(value) > limit else "")


def _parse_so360(html: str, limit: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "lxml")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in soup.select("li.res-list"):
        link = block.select_one("a[data-mdurl]")
        heading = block.select_one("h3")
        if not heading:
            continue
        # data-mdurl 是 360 自己塞在结果链接上的真实地址；没有它就只能拿到
        # so.com/link?m=... 那串加密跳转，既认不出域名，也点不进原文。
        url = (link.get("data-mdurl") if link else "") or ""
        if not url.startswith("http") or url in seen or _host(url) in JUNK_HOSTS:
            continue
        title = _squash(heading, 180)
        if not title:
            continue
        seen.add(url)
        snippet = ""
        for node in block.select(".res-desc, .res-rich, .res-comm-con, p"):
            text = SITE_FEEDBACK.sub("", _squash(node)).strip()
            if text:
                snippet = text
                break
        hits.append({
            "title": title,
            "url": url,
            "snippet": snippet,
            "source": f"网页 · {_host(url)}",
            "published": "",
        })
        if len(hits) >= limit:
            break
    return hits


async def so360(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """360 搜索。国内网络下唯一对「多词中文」不做降级的通用网页源。

    Bing 的 RSS 出口在中文查询上会退化成"只按第一个词出结果"——搜「纳米抗体 亲和标签」
    返回的全是"纳米是什么长度单位"；360 不会。它的结果页是纯服务端渲染，不需要 JS，
    也不需要 cookie。百度会跳验证码、搜狗只返回 JS 空壳，两者都当不了主力。

    但它会限流，而且是**间歇性**的：连续搜几次之后，改返回一个 6KB 左右的页面
    （带 captcha 脚本、一条 res-list 都没有），HTTP 状态还是 200。这种页面解析出来
    当然是空，"失败"和"没有结果"在界面上无法区分。所以这里显式识别并熔断五分钟，
    期间不再请求它——省下每次四五秒，交给 Bing 兜底。
    """
    global SO360_COOLDOWN_UNTIL
    if time.monotonic() < SO360_COOLDOWN_UNTIL:
        raise RuntimeError("360 刚触发过风控，冷却五分钟内跳过该源")
    params = urlencode({"q": query, "pn": "1"})
    response = await request(f"{SO360_ENDPOINT}?{params}", headers=BROWSER_HEADERS,
                             timeout=18, max_bytes=3_000_000)
    if _so360_blocked(response.text):
        SO360_COOLDOWN_UNTIL = time.monotonic() + SO360_COOLDOWN
        raise RuntimeError("360 返回的是风控拦截页（本机出口被限流），五分钟内跳过该源")
    return _parse_so360(response.text, limit)


DUCK_ENDPOINT = "https://html.duckduckgo.com/html/"


def _unwrap_ddg(url: str) -> str:
    """DDG 的结果链接是 //duckduckgo.com/l/?uddg=<encode>，真实地址在 uddg 里。"""
    if "uddg=" not in url:
        return url
    probe = url if url.startswith("http") else "https:" + url
    try:
        value = (parse_qs(urlsplit(probe).query).get("uddg") or [""])[0]
    except ValueError:
        return url
    return value if value.startswith("http") else url


def _parse_ddg(html: str, limit: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "lxml")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in soup.select("div.result"):
        classes = " ".join(block.get("class") or [])
        if "result--ad" in classes:                  # 广告位，标题往往还像正文
            continue
        link = block.select_one("a.result__a")
        if link is None:
            continue
        url = _unwrap_ddg((link.get("href") or "").strip())
        if not url.startswith("http") or url in seen:
            continue
        title = _squash(link, 180)
        if not title:
            continue
        seen.add(url)
        hits.append({
            "title": title,
            "url": url,
            "snippet": _squash(block.select_one(".result__snippet")),
            "source": f"网页 · {_host(url)}",
            "published": "",
        })
        if len(hits) >= limit:
            break
    return hits


async def duckduckgo(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """DuckDuckGo 的纯 HTML 出口。

    只在 resolve_overseas() 判为可达时才挂上来：直连必失败，硬发只会白等一次超时，
    还会往 errors 里塞一条噪音。它的价值是补 360/Bing 都覆盖不到的英文内容和
    海外站点，排序不占优势（source 仍记作「网页 · 域名」，靠 _authority 压权重）。
    """
    params = urlencode({"q": query, "kl": "wt-wt"})
    response = await request(f"{DUCK_ENDPOINT}?{params}", headers=DUCK_HEADERS,
                             timeout=12, max_bytes=3_000_000)
    if response.status == 202:                       # 202 = 人机验证页，不是空结果
        return []
    return _parse_ddg(response.text, limit)


WIKI_REST = "https://zh.wikipedia.org"
# 维基的 bot 策略很硬：UA 里必须有一个"能联系上"的地址，只写产品名一律 403
# （"Please respect our robot policy"）。`.invalid` 是 RFC 2606 保留域，明确表示
# 这个地址不可投递——比编一个真域名诚实，也能过它的格式检查。
WIKI_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "HermesResearchRadar/0.1 (local research archive; radar@localhost.invalid)",
}


async def wikipedia(query: str, limit: int = 2) -> list[dict[str, Any]]:
    """中文维基：REST 搜索，一次请求返回标题 + 定义片段。

    不用 `action=query&generator=search`：那个端点被 bot 策略拦成 403，而
    `/w/rest.php/v1/search/page` 放行。也不再逐条取 summary——两个 RTT 加起来
    要 3 秒，在 6.5 秒的检索预算里经常来不及，而 excerpt 已经带着定义
    （"单域抗体是指包含抗体中单个可变域的片段…"），模型够用了。
    只取 2 条且权重低于文献库：百科给的是背景，不是一手来源。
    """
    params = {"q": query, "limit": str(max(limit, 2))}
    found = await request(f"{WIKI_REST}/w/rest.php/v1/search/page?{urlencode(params)}",
                          headers=WIKI_HEADERS, timeout=9, max_bytes=2_000_000)
    hits: list[dict[str, Any]] = []
    for page in found.json().get("pages") or []:
        title = str(page.get("title") or "").strip()
        if not title:
            continue
        # excerpt 里包着 <span class="searchmatch"> 高亮，得先剥标签再合并空格，
        # 否则「单域 抗体」会被切开，后面按 bigram 算覆盖率时这一半就丢了。
        excerpt = BeautifulSoup(str(page.get("excerpt") or ""), "lxml").get_text("")
        summary = _clean(" ".join(part for part in (str(page.get("description") or "").strip(),
                                                    excerpt.strip()) if part))
        if not summary:
            continue
        hits.append({
            "title": title,
            "url": f"{WIKI_REST}/wiki/{quote(title, safe='')}",
            "snippet": summary,
            "source": "维基百科",
            "published": "",
        })
        if len(hits) >= limit:
            break
    return hits


CROSSREF_COOLDOWN_UNTIL = 0.0


async def crossref(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Crossref：按 DOI 收录的元数据，覆盖 Europe PMC 没有的化学、物理、工程与中文期刊。"""
    global CROSSREF_COOLDOWN_UNTIL
    if time.monotonic() < CROSSREF_COOLDOWN_UNTIL:
        raise RuntimeError("Crossref 刚返回过 429，冷却一分钟内不再请求")
    params = {
        "query.bibliographic": query,
        "rows": str(max(limit, 3)),
        "select": "title,DOI,abstract,issued,container-title",
    }
    # Crossref 有"礼貌池"：带联系方式的请求配额高得多，不带就容易 429。
    # 复用设置里填过的论文邮箱；没填也不影响功能，只是配额低一些。
    contact = _contact_email()
    if contact:
        params["mailto"] = contact
    url = f"https://api.crossref.org/works?{urlencode(params)}"
    try:
        response = await request(url, timeout=12, max_bytes=4_000_000,
                                 headers={"Accept": "application/json",
                                          "User-Agent": (f"HermesResearchRadar/0.1 (mailto:{contact})"
                                                         if contact else "HermesResearchRadar/0.1")})
    except Exception as exc:                              # noqa: BLE001 - 429 要单独认，其余照抛
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            # 撞一次就退避，别让后面每一次问答都去撞同一个墙。
            CROSSREF_COOLDOWN_UNTIL = time.monotonic() + 60
            raise RuntimeError("Crossref 限流（429），一分钟内跳过该源") from exc
        raise
    payload = json.loads(response.text)
    hits: list[dict[str, Any]] = []
    for row in (payload.get("message") or {}).get("items") or []:
        doi = (row.get("DOI") or "").strip()
        titles = row.get("title") or []
        title = _clean(titles[0] if titles else "", 180)
        if not doi or not title:
            continue
        journal = ((row.get("container-title") or [""])[0] or "").strip()
        parts = (row.get("issued") or {}).get("date-parts") or [[""]]
        year = str((parts[0] or [""])[0] or "")
        hits.append({
            "title": title,
            # DOI 优先给 doi.org：出版商自己的链接三天两头换，DOI 才是稳定标识。
            "url": f"https://doi.org/{doi}",
            "snippet": _clean(row.get("abstract") or title, SNIPPET_CHARS),
            "source": " · ".join(x for x in ("Crossref", journal, year) if x),
            "published": year,
        })
        if len(hits) >= limit:
            break
    return hits


async def europe_pmc(query: str, limit: int = 4) -> list[dict[str, Any]]:
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           f"query={quote_plus(query)}&format=json&pageSize={max(limit, 4)}&resultType=core")
    # 国际链路抖动区间是 3-14 秒（快的时候 3 秒就回）。给 6 秒：拿得到就拿，
    # 拿不到交给 Crossref 兜底；让它自己挂到 15 秒只会白占一次问答的时间。
    response = await request(url, timeout=6, headers={"Accept": "application/json"},
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


ARXIV_COOLDOWN_UNTIL = 0.0


async def arxiv(query: str, limit: int = 3) -> list[dict[str, Any]]:
    global ARXIV_COOLDOWN_UNTIL
    if time.monotonic() < ARXIV_COOLDOWN_UNTIL:
        raise RuntimeError("arXiv 刚返回过 429，冷却两分钟内不再请求")
    search = quote_plus(query)
    url = (f"https://export.arxiv.org/api/query?search_query=all:{search}"
           f"&start=0&max_results={max(limit, 3)}&sortBy=relevance")
    try:
        response = await request(url, timeout=8, max_bytes=4_000_000)
    except Exception as exc:                          # noqa: BLE001 - 429 要单独认
        if "429" in str(exc):
            ARXIV_COOLDOWN_UNTIL = time.monotonic() + 120
            raise RuntimeError("arXiv 限流（429），两分钟内跳过该源") from exc
        raise
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
        return {"science": [], "web": "", "variants": []}
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
    variants = _expand_queries(base, science)
    # 三四个字母的查询在文献库里歧义极大：ADC 同时是模数转换器、阿拉伯裔美国人
    # 协会、抗体偶联药物；搜「ADC 偶联」时前两者的论文会占满结果。这种情况下用
    # 展开的全称替换掉短查询，而不是两条都发——短的那条会把噪声顶上来。
    long_forms = [item for item in variants if not CJK_RUN.search(item) and len(item) > 8]
    if long_forms and science and len(science[0]) <= 4:
        science[0] = long_forms[0]
    return {"science": science[:2], "web": web, "variants": variants}


SOURCE_WEIGHT = (("Europe PMC", 1.05), ("Crossref", 1.0), ("维基百科", 0.95), ("arXiv", 0.9))
# 这两个是"有更好、没有也行"的补充源。它们返回空很常见（维基对中文专有名词
# 覆盖有限、DDG 偶尔判限流），记进 errors 只会让界面显示"覆盖可能不完整"，
# 把用户的注意力引到一个其实没影响的地方。
OPTIONAL_SOURCES = {"DuckDuckGo", "维基百科"}

# 域名权威度。DeepSeek 的搜索 API 把 authority 单独列成一个 0-4 的字段，
# 因为同一个查询里"微博热搜"和"学校官网通知"的字面相关度可能一模一样，
# 差别全在可信度上。这里用一张很小的表做同义的事，不需要模型。
LOW_AUTHORITY_HOSTS = (
    "weibo.com", "weibo.cn", "douyin.com", "xiaohongshu.com", "zhihu.com",
    "baijiahao.baidu.com", "toutiao.com", "csdn.net", "jianshu.com",
    "51cto.com", "cnblogs.com", "bilibili.com", "ixigua.com", "kuaishou.com",
)
HIGH_AUTHORITY_HOSTS = (
    "arxiv.org", "nature.com", "science.org", "cell.com", "nih.gov", "who.int",
    "europepmc.org", "ncbi.nlm.nih.gov", "doi.org", "sciencedirect.com",
    "springer.com", "wiley.com", "oup.com", "acs.org", "rsc.org",
)


def _authority(url: str) -> float:
    host = _host(url)
    if not host:
        return 1.0
    for blocked in LOW_AUTHORITY_HOSTS:
        if host == blocked or host.endswith("." + blocked):
            return 0.5
    if host.endswith((".gov.cn", ".edu.cn", ".edu", ".gov", ".ac.uk", ".ac.jp", ".org.cn")):
        return 1.2
    for trusted in HIGH_AUTHORITY_HOSTS:
        if host == trusted or host.endswith("." + trusted):
            return 1.15
    return 1.0


def _cjk_grams(text: str, size: int = 2) -> list[str]:
    """把连续汉字切成 bigram：'纳米抗体' → 纳米 / 米抗 / 抗体。

    没有分词器时这是唯一稳的中文匹配单位。整段连续汉字（旧做法）要求"纳米抗体"
    这四个字原样出现在标题里，标题写成"纳米抗体的亲和标签"能过，写成"两种纳米
    抗体"就断成两段被判不相关——这就是网页结果常年被全部丢弃的直接原因。
    """
    grams: list[str] = []
    for run in CJK_RUN.findall(text or ""):
        if len(run) <= size:
            grams.append(run)
        else:
            grams.extend(run[i:i + size] for i in range(len(run) - size + 1))
    return list(dict.fromkeys(grams))


def _terms_of(text: str) -> list[str]:
    words = [w.lower() for w in LATIN_TOKEN.findall(text) if w.lower() not in STOP_TOKENS]
    words += _cjk_grams(text)
    return list(dict.fromkeys(words))


def _overlap(hit: dict[str, Any], query: str) -> float:
    """这条结果对「发给这个源的查询」的覆盖率。

    拉丁词和汉字分开算再平均，不能混进同一个分母：查询「PROTAC 降解剂 最新进展」
    里只有一个英文词，却有五个汉字 bigram，混着算时命中 PROTAC 也只是 1/6。
    """
    latin = list(dict.fromkeys(w.lower() for w in LATIN_TOKEN.findall(query)
                               if w.lower() not in STOP_TOKENS))
    grams = _cjk_grams(query)
    text = f"{hit.get('title') or ''} {hit.get('snippet') or ''}".lower()
    parts: list[float] = []
    if latin:
        parts.append(sum(1 for word in latin if word in text) / len(latin))
    if grams:
        parts.append(sum(1 for gram in grams if gram in text) / len(grams))
    return sum(parts) / len(parts) if parts else 0.0


def _source_weight(hit: dict[str, Any]) -> float:
    for prefix, weight in SOURCE_WEIGHT:
        if hit["source"].startswith(prefix):
            return weight
    return 0.75 if hit["source"].startswith("网页") else 0.6


# 语义相关的下限，低于它仍按字面判。这两个数是拿真实查询试出来的：
# bge-small-zh 上同主题标题 0.62–0.75，不相干的 0.28–0.38。定太高会漏，
# 定太低会让「同领域但不同问题」的文章挤进来，靠 _rank 排序也压不住。
SEMANTIC_FLOOR = {"science": 0.62, "web": 0.55}


def _semantic_scores(collected: list[tuple[str, str, dict[str, Any]]]) -> list[float] | None:
    """给每条候选算「和查询的语义相似度」。算不了就返回 None。

    这一步专门治字面匹配的死角：搜「ADC 偶联」时，标题写「抗体药物偶联物」
    的论文 bigram 覆盖率是 0，会被判成不相关直接扔掉；语义分能到 0.7 以上。
    """
    if not collected:
        return None
    try:
        from . import embedding
        if not embedding.available():
            return None
        queries = list(dict.fromkeys(query for _, query, _ in collected))
        texts = [f"{hit.get('title') or ''}。{str(hit.get('snippet') or '')[:300]}"
                 for _, _, hit in collected]
        vectors = embedding.encode(queries + texts)
        if not vectors or len(vectors) != len(queries) + len(texts):
            return None
        by_query = {query: vectors[index] for index, query in enumerate(queries)}
        base = len(queries)
        return [embedding.cosine(by_query[query], vectors[base + index])
                for index, (_, query, _) in enumerate(collected)]
    except Exception:                            # noqa: BLE001 - 语义只是加分项
        return None


def _unique_queries(queries: list[str]) -> list[str]:
    """合并被包含的查询。

    plan_queries 会给出「nanobody affinity」和「nanobody affinity tag」这种关系：
    后者包含前者，两个都发等于对同一个文献库连发两次高度重叠的请求，命中几乎一样，
    却把 Europe PMC 的配额和时间翻倍用掉。
    """
    kept: list[str] = []
    for query in queries:
        words = set(query.lower().split())
        merged = False
        for index, existing in enumerate(kept):
            existing_words = set(existing.lower().split())
            if words <= existing_words:
                merged = True
                break
            if existing_words < words:
                kept[index] = query          # 用信息更多的那个替换短的
                merged = True
                break
        if not merged:
            kept.append(query)
    return kept


async def gather(question: str, *, item_title: str = "", limit: int = 10,
                 allow_web: bool = True, web_limit: int = 5,
                 item_terms: list[str] | None = None,
                 read_pages: bool = True) -> dict[str, Any]:
    """并发跑所有源，去重、排序、抓正文、截断，返回可直接喂给模型的结构。

    三段式，和 DeepSeek / Perplexity 的联网链路同构：
    **召回**（多源并发）→ **重排**（词面覆盖 + 来源权重）→ **读正文**（前几条抓回
    全文、按查询挑段落）。只做前两段时模型只看到标题和一两句摘要，回答自然浮在表面。

    核心原则：**只排序，不乱丢**。以前按阈值把"不够像"的结果成批丢掉，中文多词
    查询几乎必然全军覆没，模型于是答"我搜不到"，用户看到的就是"联网不好使"。
    """
    started = time.monotonic()
    plan = plan_queries(question, item_title, item_terms)
    plan["science"] = _unique_queries(plan["science"])
    web_base = (plan["web"] or "").lower()
    plan["variants"] = [item for item in _unique_queries(plan.get("variants") or [])
                        if item.lower() != web_base]
    if not plan["science"] and not plan["web"]:
        return {"queries": [], "results": [], "errors": [], "notes": [],
                "elapsed_ms": 0, "fetched": 0}

    tasks: list[tuple[str, str, str, Any]] = []
    for query in plan["science"]:
        tasks.append(("science", query, f"Europe PMC:{query}", europe_pmc(query, 3)))
        tasks.append(("science", query, f"arXiv:{query}", arxiv(query, 2)))
        tasks.append(("science", query, f"Crossref:{query}", crossref(query, 2)))
    if allow_web and plan["web"]:
        # 两个通用网页源分工不同：360 扛中文多词，Bing 扛英文与站点直达。
        # 谁挂了都不影响另一个，errors 里会留下原因。
        tasks.append(("web", plan["web"], f"360:{plan['web']}", so360(plan["web"], 6)))
        tasks.append(("web", plan["web"], f"Bing:{plan['web']}", bing(plan["web"], 6)))
        # 变体交给 Bing 而不是 360：Bing 稳定（360 会限流），而且换一种说法
        # 正好绕开「Bing 中文多词只看首词」那个老毛病——首词换了，结果集就换了。
        if plan["variants"]:
            tasks.append(("web", plan["variants"][0], f"Bing变体:{plan['variants'][0]}",
                          bing(plan["variants"][0], 4)))
        # 海外源按本机连通性挂载：有代理（或本机本来就在墙外）才加 DDG 与维基。
        # 探测结果缓存 3 分钟；没代理的机器这里只是一次毫秒级的端口扫描，
        # 不会因为"没有梯子"就让整条检索链失败。
        overseas = False
        try:
            from ..net import resolve_overseas
            overseas = bool((await asyncio.to_thread(resolve_overseas)).get("overseas"))
        except Exception:                            # noqa: BLE001 - 探不到就按出不去处理
            overseas = False
        if overseas:
            # DDG 用英文主题词而不是用户原话：它的强项是英文网页，拿中文长句去搜
            # 既搜不到东西，回来的一堆英文结果还会因为"跟中文查询没有字面重合"
            # 被 _overlap 判成 0 分丢掉。打分也用同一个查询，口径才对得上。
            ddg_query = (plan["science"][0] if plan["science"] else plan["web"])
            tasks.append(("web", ddg_query, f"DuckDuckGo:{ddg_query}",
                          duckduckgo(ddg_query, 5)))
            tasks.append(("web", plan["web"], f"维基百科:{plan['web']}",
                          wikipedia(plan["web"], 2)))

    # 一个慢源不该拖死整次问答：Europe PMC 从这台机器出去有时 3 秒、有时 14 秒。
    # 到点就用手上已有的，慢的那个记一条 error 说明，用户至少能立刻看到结果。
    # 预算从源真正开跑算起，不让代理探测那一下吃掉检索时间。
    futures = [asyncio.ensure_future(coro) for *_, coro in tasks]
    done, pending = await asyncio.wait(futures, timeout=SEARCH_BUDGET)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    errors: list[str] = []
    # 先只做去重和收集，打分放到后面统一算：语义向量要一次性批量编码，
    # 逐条调 encode 会把「40 条标题」拆成 40 次推理，白白多花一个数量级的时间。
    collected: list[tuple[str, str, dict[str, Any], str]] = []
    seen: set[str] = set()
    for (bucket, query, label, _), future in zip(tasks, futures):
        name = label.split(":", 1)[0]
        if future not in done or future.cancelled():
            errors.append(f"{name} 超过 {SEARCH_BUDGET:.0f} 秒没返回，已跳过")
            continue
        outcome = future.exception()
        if outcome is not None:
            errors.append(f"{name} 失败：{type(outcome).__name__}: {str(outcome)[:120]}")
            continue
        rows = future.result() or []
        if not rows:
            if name not in OPTIONAL_SOURCES:
                errors.append(f"{name} 没有返回结果")
            continue
        head_words = [w.lower() for w in LATIN_TOKEN.findall(query)
                      if w.lower() not in STOP_TOKENS][:1]
        head = head_words[0] if head_words else ""
        for hit in rows:
            # arXiv 同一篇有带版本号和不带版本号两种写法，不归一就会重复占位。
            key = re.sub(r"v\d+$", "", re.sub(r"^https?://(www\.)?", "",
                                                hit["url"]).rstrip("/").lower())
            title_key = re.sub(r"[\W_]+", "", hit["title"].lower())[:40]
            # 同一篇文章在 Europe PMC 与 Crossref 里都有，标题去重比 URL 去重更管用。
            if key in seen or (title_key and title_key in seen):
                continue
            seen.add(key)
            if title_key:
                seen.add(title_key)
            collected.append((bucket, query, hit, head))

    semantic = await asyncio.to_thread(_semantic_scores, [(b, q, h) for b, q, h, _ in collected])

    buckets: dict[str, list[dict[str, Any]]] = {"science": [], "web": []}
    weak: list[dict[str, Any]] = []
    for index, (bucket, query, hit, head) in enumerate(collected):
        overlap = _overlap(hit, query)
        text = f"{hit['title']} {hit['snippet']}".lower()
        similar = semantic[index] if semantic else 0.0
        floor = SEMANTIC_FLOOR.get(bucket, 0.6)
        if bucket == "science":
            # 文献库是相关性打分：词越多越糊，太低的确实不是同一件事。
            ok = overlap >= 0.45 and (not head or head in text)
        else:
            # 网页源放到 0.25：360 的标题里常把关键词改写或拆开，门槛一高就全没了。
            ok = overlap >= 0.25
        # 字面判死不代表不是同一件事：ADC ↔ 抗体药物偶联物、VHH ↔ 纳米抗体
        # 在 bigram 上覆盖率是 0，语义分却很高。任一路径过关就留下。
        if not ok and similar >= floor:
            ok = True
        combined = overlap if not semantic else (0.45 * overlap + 0.55 * similar)
        row = {**hit, "_rank": combined * _source_weight(hit) * _authority(hit["url"]),
               "semantic": round(similar, 3) if semantic else None}
        if ok:
            buckets[bucket].append(row)
        else:
            weak.append(row)

    for bucket in buckets.values():
        bucket.sort(key=lambda h: h["_rank"], reverse=True)
    weak.sort(key=lambda h: h["_rank"], reverse=True)

    science_quota = max(limit - web_limit, 0)
    ordered = buckets["science"][:science_quota]
    # 文献源没占满的名额让给网页，别让"5 条文献上限"把可用的网页结果也挤掉。
    ordered += buckets["web"][:max(web_limit, limit - len(ordered))]
    # 保底只在"一条都不剩"时启用，且只补 3 条：有真结果时硬塞弱相关内容
    # 会把"西湖大学直博"变成"西湖旅游攻略"，比少给几条更糟。
    if not ordered:
        ordered = weak[:3]
        if ordered:
            errors.append("没有强相关结果，已按相关度取前几条兜底，请谨慎采信")

    results: list[dict[str, Any]] = []
    used = 0
    # 抓过正文的条目一条就有一千多字，预算按"能装下 limit 条正文"来定。
    # 卡在 10000 时后半截会被整体挤掉，等于白抓——现在最多 8 页，放到 16000。
    budget = 16000
    for hit in ordered:
        cost = len(hit["title"]) + len(hit["snippet"])
        if used + cost > budget and results:
            continue
        used += cost
        clean = {k: v for k, v in hit.items() if not k.startswith("_")}
        results.append({**clean, "n": len(results) + 1})
        if len(results) >= limit:
            break

    fetched = 0
    notes: list[str] = []
    if read_pages and results:
        from .pagefetch import enrich
        # 用第一个 science 查询 + 网页查询的词做段落打分：前者是英文主题词，
        # 后者是用户原话，合起来才能同时抓住"这篇讲什么"和"这段有没有回答我"。
        terms = list(dict.fromkeys(_terms_of(" ".join(plan["science"][:1] + [plan["web"]]))))
        try:
            page_stats = await enrich(results, terms)
            results = page_stats["results"]
            fetched = page_stats["fetched"]
            if page_stats["skipped"]:
                # 抓不到正文只是少一点细节，不是"这个源坏了"，不能混进 errors：
                # render_block 会把 errors 翻译成"覆盖可能不完整"，把模型带偏。
                notes.append(f"{page_stats['skipped']} 条网页没抓动正文，已保留搜索摘要")
        except Exception as exc:                          # noqa: BLE001 - 抓正文失败不该毁掉检索
            notes.append(f"抓取正文失败：{type(exc).__name__}: {str(exc)[:80]}")

    return {"queries": plan["science"] + ([plan["web"]] if plan["web"] else []),
            "results": results, "errors": errors[:4], "notes": notes,
            "fetched": fetched,
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
    read = int(payload.get("fetched") or 0)
    lines = [header + "。引用时在句末标注 [序号]；检索没覆盖的就直说没有查到。"]
    if read:
        # 明确告诉模型哪些是全文：不然它会把整段正文当成搜索摘要，只敢引用第一句。
        lines.append(f"其中 {read} 条网页已抓取正文，条目里的长段落就是原文，"
                     "里面的数字、方法、结论可以直接引用，不必再说'摘要中没有提到'。")
    if payload.get("errors") and results:
        lines.append(f"注意：本次有源没成功（{'；'.join(payload['errors'][:2])}），"
                     "覆盖可能不完整。")
    for hit in results:
        head = f"[{hit['n']}] {hit['title']}"
        meta = " · ".join(x for x in (hit.get("source"), hit.get("url")) if x)
        lines.append(f"{head}\n    {meta}\n    {hit['snippet']}")
    return "\n".join(lines)
