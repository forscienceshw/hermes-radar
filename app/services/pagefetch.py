"""抓网页正文并按查询重排段落——联网问答里真正决定质量的一步。

搜索引擎只给「标题 + 一两句摘要」，数字、方法、结论这些真正有用的东西全在正文里。
只把摘要喂给模型，回答往往停在"有这么篇文章"的层面，用户体感就是"搜了跟没搜一样"。

DeepSeek / Perplexity 的联网链路是：检索 → 抓正文 → 切块重排 → 只把最相关的
几段拼进上下文。这里做的就是中间两段。正文抽取用 trafilatura（项目归档已经在用），
段落排序用和检索同一套词面覆盖打分，不引入额外依赖。

失败是常态而非异常：反爬、超时、PDF、纯 JS 渲染页都会拿不到正文。任何一页失败
都只是退回原来的摘要，绝不拖垮整次检索。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from bs4 import BeautifulSoup

from ..net import request

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 每条结果最多贡献多少字正文。再多就是稀释：模型对上下文中间部分的注意力最差。
BLOCK_CHARS = 900
# 短于这个长度的块是导航、版权声明、按钮文字，不是内容。
# 中文一句话常在 25-40 字，阈值卡到 50 会把"结论一句话"这种最关键的行误杀。
MIN_BLOCK = 28
# 抓回来不足这么多字就当没抓到——多半是验证页或空壳。
MIN_ARTICLE = 300
# 一次联网问答最多抓几页。并发抓 6 页和抓 3 页的总耗时差不多，但覆盖率差一倍。
MAX_PAGES = 4
PAGE_TIMEOUT = 5.0
TOTAL_BUDGET = 5.5

# 这些域名直接放弃：要么登录墙，要么内容在客户端渲染，抓回来只有壳。
HOPELESS_HOSTS = (
    "mp.weixin.qq.com", "zhuanlan.zhihu.com", "www.zhihu.com", "zhihu.com",
    "www.xiaohongshu.com", "xiaohongshu.com", "www.douyin.com", "v.douyin.com",
    "weibo.com", "www.weibo.com", "tieba.baidu.com", "www.jianshu.com",
    "x.com", "twitter.com", "www.facebook.com", "www.instagram.com",
)
# 期刊站点大半挂了 Cloudflare / 人机校验，抓正文纯属浪费预算；它们的摘要已经够用。
HOPELESS_HOST_SUFFIX = ("sciencedirect.com", "springer.com", "wiley.com",
                        "tandfonline.com", "sagepub.com", "nature.com", "science.org",
                        "cell.com", "academic.oup.com", "pubs.acs.org", "acs.org")


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def is_readable(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    if host in HOPELESS_HOSTS:
        return False
    for suffix in HOPELESS_HOST_SUFFIX:
        # 精确到子域或同域，别让 "notnature.com" 也被挡掉。
        if host == suffix or host.endswith("." + suffix):
            return False
    if url.lower().endswith((".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")):
        return False
    return True


def _split_blocks(text: str) -> list[str]:
    """按行切块。trafilatura 抽出来的正文段落之间就是一个换行。"""
    blocks: list[str] = []
    for raw in re.split(r"\n+", text or ""):
        value = re.sub(r"[ \t\u00a0]+", " ", raw).strip()
        if len(value) >= MIN_BLOCK:
            blocks.append(value)
    return blocks


def _score(block: str, terms: list[str]) -> float:
    if not terms:
        return 0.0
    text = block.lower()
    hit = sum(1 for term in terms if term in text)
    return hit / len(terms)


def pick_blocks(text: str, terms: list[str], limit: int = BLOCK_CHARS) -> str:
    """按相关度挑段落，但按原文顺序拼回去。

    顺序很重要：模型读被打乱顺序的段落会拼出错误的因果。所以先按分数选出要哪几段，
    再按它们在原文里的位置重排，最后截断到字数预算。

    光有字数预算不够：文章很短时预算根本用不完，噪声段会跟着混进来。所以再加一道
    **相对分数门槛**——不到最高分三分之一的段落，再空也不收。
    """
    blocks = _split_blocks(text)
    if not blocks:
        return ""
    scored = [(index, _score(block, terms), block) for index, block in enumerate(blocks)]
    best = max((row[1] for row in scored), default=0.0)
    if best <= 0:
        # 一个查询词都没命中：多半是抓到了另一个话题的页面。只给开头几段，别整篇塞进去。
        return "\n".join(blocks[:3])[:limit]
    floor = best * 0.34
    keep: list[tuple[int, str]] = []
    used = 0
    # 先拿分高的；同分时靠前的优先（导语段信息密度通常最高）。
    for index, score_value, block in sorted(scored, key=lambda row: (-row[1], row[0])):
        if score_value < floor:
            break
        if used + len(block) > limit and keep:
            continue
        keep.append((index, block))
        used += len(block)
        if used >= limit * 0.9:
            break
    keep.sort(key=lambda row: row[0])
    return "\n".join(block for _index, block in keep)[:limit]


def extract_text(html: str) -> str:
    """干净正文：先 trafilatura，抽不出来再退回 <article>/<p>。"""
    try:
        import trafilatura
        extracted = trafilatura.extract(html or "", include_comments=False,
                                        include_tables=True, favor_recall=True)
        if extracted and len(extracted) >= MIN_ARTICLE:
            return extracted
    except Exception:                                     # noqa: BLE001 - 抽取器坏了不该让整页失败
        pass
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup.select("script,style,nav,footer,header,aside,form,iframe,noscript"):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    return root.get_text("\n", strip=True)


async def read_page(url: str, terms: list[str], *, max_bytes: int = 3_000_000,
                    timeout: float = PAGE_TIMEOUT) -> dict[str, Any] | None:
    """抓一页正文并挑出相关段落。失败返回 None，调用方保留原摘要。"""
    if not is_readable(url):
        return None
    try:
        response = await request(url, headers=BROWSER_HEADERS, timeout=timeout,
                                 max_bytes=max_bytes)
    except Exception:                                     # noqa: BLE001 - 反爬/超时/证书问题都算抓不到
        return None
    content_type = (response.headers.get("content-type") or "").lower()
    if content_type and "html" not in content_type and "xml" not in content_type:
        return None
    text = extract_text(response.text)
    if len(text) < MIN_ARTICLE:
        return None
    picked = pick_blocks(text, terms)
    if len(picked) < 120:
        return None
    return {"url": str(response.url), "text": picked, "chars": len(text)}


async def enrich(results: list[dict[str, Any]], terms: list[str], *,
                 top: int = MAX_PAGES, budget: float = TOTAL_BUDGET) -> dict[str, Any]:
    """给排在前面的几条网页结果补上正文。

    原地不动 results 本身（调用方可能已经发给前端了），返回一份新的列表和统计。
    统计要带回前端：用户需要知道"抓了几页、有几页没抓动"，否则又会以为功能坏了。
    """
    picked: list[int] = []
    for index, hit in enumerate(results):
        if len(picked) >= top:
            break
        if str(hit.get("source") or "").startswith("网页") and is_readable(hit.get("url") or ""):
            picked.append(index)
    if not picked:
        return {"results": results, "fetched": 0, "skipped": 0, "chars": 0, "elapsed_ms": 0}

    started = time.monotonic()
    futures = [asyncio.ensure_future(read_page(results[index]["url"], terms))
               for index in picked]
    # 抓正文也必须有总预算：某一页卡在慢站点上不该让整次问答陪它等。
    done, pending = await asyncio.wait(futures, timeout=budget)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    enriched = [dict(hit) for hit in results]
    fetched = 0
    chars = 0
    for index, future in zip(picked, futures):
        if future not in done or future.cancelled():
            continue
        try:
            page = future.result()
        except Exception:                                 # noqa: BLE001 - 单页失败只退回摘要
            continue
        if not page:
            continue
        hit = enriched[index]
        # 正文比摘要长得多时才有替换意义；有的页面正文还没摘要全。
        if len(page["text"]) <= len(str(hit.get("snippet") or "")):
            continue
        hit["snippet"] = page["text"]
        hit["source"] = f"{hit['source']} · 已读正文"
        fetched += 1
        chars += page["chars"]
    return {"results": enriched, "fetched": fetched, "skipped": len(picked) - fetched,
            "chars": chars, "elapsed_ms": int((time.monotonic() - started) * 1000)}
