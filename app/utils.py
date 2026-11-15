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


def local_date() -> str:
    return datetime.now().astimezone().date().isoformat()


def job_error_is_retryable(message: str) -> bool:
    text = (message or "").lower()
    transient = ("timeout", "timed out", "connection", "temporarily", "temporary failure",
                 "429", "502", "503", "504", "database is locked", "gateway_stream_interrupted",
                 "quota_rate_limited", "network", "eof occurred")
    permanent = ("invalid_parameter", "invalid parameter", "keyerror", "typeerror",
                 "attributeerror", "syntaxerror", "not found", "401", "403", "404",
                 "insufficient_balance", "authentication")
    return any(token in text for token in transient) and not any(token in text for token in permanent)


def redact_secrets(text: str) -> str:
    value = re.sub(r"([?&](?:key|access_token)=)[^&\s\"']+", r"\1REDACTED", str(text))
    value = re.sub(r"(Bearer\s+)[A-Za-z0-9._-]+", r"\1REDACTED", value)
    value = re.sub(r"\bdx_[A-Za-z0-9]{12,}\b", "dx_REDACTED", value)
    return value
