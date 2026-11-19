import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\"'，。；！）】]+", re.I)
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm",
    "from", "scene", "subscene", "clicktime", "enterid", "ascene", "fasttmpl_type",
    "share_source", "share_medium", "share_plat", "share_session_id", "timestamp",
    "unique_k", "code", "state", "fbclid", "gclid", "spm_id_from", "vd_source",
    "share_tag", "bbid", "from_spmid", "plat_id", "share_from",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="milliseconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def epoch_iso(value: int | float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def canonical_url(url: str) -> str:
    url = url.strip()
    bvid = extract_bvid(url)
    if bvid:
        return f"https://www.bilibili.com/video/{bvid}/"
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return url
    if host == "mp.weixin.qq.com":
        # The same article is shared with different sn/chksm/mpshare parameters and
        # sometimes over http. __biz+mid+idx is the only stable identity.
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if query.get("__biz") and query.get("mid"):
            kept = [("__biz", query["__biz"]), ("mid", query["mid"]), ("idx", query.get("idx") or "1")]
        elif query.get("sn"):
            kept = [("sn", query["sn"])]
        elif query.get("__biz"):
            kept = [("__biz", query["__biz"])]
        else:
            kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if k.lower() not in TRACKING_KEYS]
        return urlunsplit(("https", "mp.weixin.qq.com", parts.path or "/", urlencode(kept, doseq=True), ""))
    port = parts.port
    netloc = host if not port or (scheme == "https" and port == 443) or (scheme == "http" and port == 80) else f"{host}:{port}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_KEYS]
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def extract_bvid(text: str) -> str | None:
    match = BVID_RE.search(text or "")
    return match.group(0) if match else None


def extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    result = []
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(".,;:!?)]}")
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def normalize_doi(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I)
    match = DOI_RE.search(value)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}>。，；").lower()


def extract_dois(text: str) -> list[str]:
    candidates = DOI_RE.findall((text or "").replace("\\/", "/"))
    result = []
    seen = set()
    for candidate in candidates:
        doi = normalize_doi(candidate)
        if doi and doi not in seen:
            seen.add(doi)
            result.append(doi)
    return result


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def safe_name(value: str, limit: int = 100) -> str:
    value = unicodedata.normalize("NFKC", value or "untitled")
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" .")
    return (value or "untitled")[:limit]


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


# <img alt> is sometimes a real caption ("图1：三维壳层法与局部法向法的几何区别"),
# sometimes just a filename or a note to the editor ("图片13.png"、"公众号固定装饰图3").
_ALT_FILENAME_RE = re.compile(r"^[\w\-. ]+\.(png|jpe?g|gif|webp|bmp|svg)$", re.I)
_ALT_JUNK_MARKERS = ("固定装饰", "装饰图", "原文图组", "分割线", "二维码", "占位图")


def clean_image_alt(text: str | None) -> str:
    """Return a caption-looking alt text, or '' when it carries no information."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) < 8 or value.casefold() in {"图片", "图像", "图", "image", "photo", "img", "picture"}:
        return ""
    if _ALT_FILENAME_RE.match(value) or re.fullmatch(r"图片\s*\d*", value):
        return ""
    if any(marker in value for marker in _ALT_JUNK_MARKERS):
        return ""
    return value[:300]



def head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return text[:head] + f"\n\n[中间 {len(text) - limit} 字未送入本次模型，完整原文仍已本地保存]\n\n" + text[-tail:]


def lexical_score(text: str, keywords: list[str], excluded: list[str] | None = None) -> float:
    lowered = (text or "").lower()
    hits = sum(1 for word in keywords if word.lower() in lowered)
    score = min(100.0, hits * 20.0)
    for word in excluded or []:
        if word.lower() in lowered:
            score = max(0.0, score - 20.0)
    return score


# 「批量归档」的暂停开关，存在 runtime 表里。API 置位，JobRunner 每个条目之间读一次；
# 手动再次点「归档全部」会清掉它。放在这里是因为 api 和 pipeline 都 import utils，
# 而 pipeline 不能反向 import api（会成环）。
ARCHIVE_PAUSE_KEY = "archive_batch_paused"

# 公众号名比对时要折叠的 OCR 易混字符：小写 L / 数字 1 / 竖线 都会被认成大写 I，
# 数字 0 会被认成字母 O。公众号库里的名字有一批是 OCR 出来的（``Al-Protein
# Design`` 其实是 ``AI-Protein Design``，``ScienceAl`` 是 ``ScienceAI``），
# 不折叠就永远对不上 gh 编号，那些号也就永远进不了追踪名单。
_OCR_CONFUSABLES = {"l": "i", "1": "i", "|": "i", "0": "o"}
_OCR_DROP = ".-_·—–()（）[]【】:：/、,"


def name_match_key(value: Any) -> str:
    """公众号名比对键：NFKC + 去空白 + 去分隔符 + 折叠 OCR 易混字符。

    只能当**退一步**的匹配用：先精确、再包含、最后才用它。因为它把 ``l`` 折成
    ``i`` 之后，「Cell」和「Ceii」会撞在一起，所以调用方必须要求唯一命中。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = "".join(text.split())
    for ch in _OCR_DROP:
        text = text.replace(ch, "")
    for bad, good in _OCR_CONFUSABLES.items():
        text = text.replace(bad, good)
    return text


def local_date() -> str:
    return datetime.now().astimezone().date().isoformat()


_HTML_HEAD_RE = re.compile(r"<(!doctype|html|head|body|meta|title)\b", re.I)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def humanize_error_text(text: str, limit: int = 1500) -> str:
    """把原始报错变成一句人话，绝不让整页 HTML 落进 items.error。

    正常情况下接口错误是 JSON。但网关抽风时会直接返回一整页 HTML（腾讯 COS 的
    「WAF拦截页面」就是这样），原样存库等于在资料库里躺 8000 字网页源码。
    """
    value = (text or "").strip()
    if not value:
        return ""
    if not _HTML_HEAD_RE.search(value[:800]):
        return value[:limit]
    match = _HTML_TITLE_RE.search(value[:4000])
    label = re.sub(r"\s+", " ", match.group(1)).strip()[:40] if match else "网页"
    head = value[:4000]
    if "WAF" in head.upper() or "拦截" in head or "BLOCK" in head.upper():
        return (f"接口返回了「{label}」而不是数据，通常是网关临时风控或限流，"
                f"过几分钟重试即可；内容本身没有问题")
    return f"接口返回了网页（{label}）而不是数据，可能是网关异常，稍后重试"


def job_error_is_retryable(message: str) -> bool:
    text = (message or "").lower()
    transient = ("timeout", "timed out", "connection", "temporarily", "temporary failure",
                 "429", "502", "503", "504", "database is locked", "gateway_stream_interrupted",
                 "quota_rate_limited", "network", "eof occurred",
                 "超时", "连接失败", "连接被重置", "网络异常", "网络错误",
                 "临时风控", "网关临时", "稍后重试", "过几分钟重试")
    permanent = ("invalid_parameter", "invalid parameter", "keyerror", "typeerror",
                 "attributeerror", "syntaxerror", "not found", "401", "403", "404",
                 "insufficient_balance", "authentication")
    return any(token in text for token in transient) and not any(token in text for token in permanent)


def job_error_is_environmental(message: str) -> bool:
    """这类错误不是任务本身坏了，是这台机器/这个通道还没就绪。

    微信没登录、当前只读适配器拒绝 4.x 的库、外部数据源没启用——等环境恢复
    下一轮自然就好。把它们每轮都写成一条 DEAD，失败列表就会变成日志垃圾场
    （本机实测 813 条死信里 786 条都是「微信没识别出账号」）。
    """
    text = str(message or "").lower()
    tokens = ("未能识别当前微信账号", "请先打开并登录微信", "请保持微信已登录",
              "不会复用 3.x 密钥", "未检测到微信", "微信未运行", "微信没有运行",
              "数据源未启用", "余额不足", "余额约", "等待充值")
    return any(token.lower() in text for token in tokens)


def redact_secrets(text: str) -> str:
    value = re.sub(r"([?&](?:key|access_token)=)[^&\s\"']+", r"\1REDACTED", str(text))
    value = re.sub(r"(Bearer\s+)[A-Za-z0-9._-]+", r"\1REDACTED", value)
    value = re.sub(r"\bdx_[A-Za-z0-9]{12,}\b", "dx_REDACTED", value)
    return value
