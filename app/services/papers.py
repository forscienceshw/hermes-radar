import asyncio
import difflib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from pypdf import PdfReader

from ..db import Database
from ..net import download_file, get_json, request
from ..settings import get_settings
from ..storage import atomic_write, paper_folder
from . import purge
from .archive import raw_file, read_raw_bytes
from ..utils import extract_dois, json_loads, now_iso, stable_json

# 标题相似度低于这个值就当没找到——宁可留着「未发现论文」，也不能挂错一篇。
TITLE_SIMILARITY = 0.86
# nature.com/articles/s41893-026-01986-6 的后半段就是 DOI，公众号文末常贴这种链接。
NATURE_ARTICLE_RE = re.compile(r"https?://(?:www\.)?nature\.com/articles/([A-Za-z0-9._\-]+)", re.I)
QUOTED_RE = re.compile(r"[“\"「『《]([^”\"」』》]{18,220})[”\"」』》]")


def doi_from_links(text: str) -> list[str]:
    """从正文里的出版社链接反推 DOI。

    公众号习惯在文末贴原文地址，而 `nature.com/articles/s41893-026-01986-6`
    这种链接里没有「10.xxxx/」，`extract_dois` 的正则认不出来，但后缀就是 DOI。
    只做确定性映射，猜不准的形态一律不碰。
    """
    found: list[str] = []
    for match in NATURE_ARTICLE_RE.finditer(text or ""):
        suffix = match.group(1).strip(".,;）)")
        if len(suffix) >= 8 and not suffix.startswith("10."):
            found.append(f"10.1038/{suffix}".lower())
    return list(dict.fromkeys(found))


def _norm_title(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def title_similarity(left: str, right: str) -> float:
    """标题比对：词覆盖率与整串相似度取高者，长标题缩写也能对上。"""
    a, b = _norm_title(left), _norm_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(overlap, difflib.SequenceMatcher(None, a, b).ratio())


# 期刊名经常也被引号括起来（「《Accounts of Chemical Research》」），别拿它当论文标题去查。
JOURNAL_LIKE_RE = re.compile(
    r"^(journal of|proceedings of|frontiers in|accounts of|annual review of|advances in|"
    r"nature|science|cell|the lancet|bmj|pnas|elife|chemical reviews|chemical society reviews)\b",
    re.I)


def _looks_like_paper_title(span: str) -> bool:
    span = span.strip()
    if not 18 <= len(span) <= 220:
        return False
    lowered = span.lower()
    if "http" in lowered or "@" in span:
        return False
    if JOURNAL_LIKE_RE.match(span) and len(span.split()) <= 5:
        return False
    ascii_ratio = sum(ch.isascii() for ch in span) / max(1, len(span))
    if ascii_ratio < 0.85:
        # 中文标题交给正文关键词判断，不在这里猜——中文重名太多，猜错代价更大。
        return False
    return len(re.findall(r"[A-Za-z][A-Za-z'\-]+", span)) >= 4


def candidate_titles(text: str, limit: int = 4) -> list[str]:
    """挑出正文里「像是论文标题」的英文片段。

    公众号的写法很固定：研究报道“Title”、题为《Title》、发表在《Journal》的“Title”。
    所以只认引号/书名号里的片段，不做整段英文识别——那样会把句子里任意一段当标题。
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in QUOTED_RE.finditer(text or ""):
        span = re.sub(r"\s+", " ", match.group(1)).strip()
        key = span.casefold()
        if key in seen or not _looks_like_paper_title(span):
            continue
        seen.add(key)
        out.append(span)
        if len(out) >= limit:
            break
    return out


async def _title_candidates(title: str, email: str) -> list[dict[str, Any]]:
    """按标题找 DOI。Crossref 优先，Europe PMC 兜底，相似度不够的一律丢掉。"""
    hits: list[dict[str, Any]] = []
    params = {"query.bibliographic": title, "rows": "5", "select": "DOI,title", "mailto": email or ""}
    try:
        payload, _ = await get_json("https://api.crossref.org/works?" + urlencode(params),
                                    headers={"Accept": "application/json"}, max_bytes=2_000_000)
        for row in (payload.get("message") or {}).get("items") or []:
            name = (row.get("title") or [""])[0]
            doi = str(row.get("DOI") or "").strip().lower()
            if doi and name:
                hits.append({"doi": doi, "title": name, "source": "Crossref",
                             "similarity": title_similarity(title, name)})
    except Exception:
        pass
    if not any(hit["similarity"] >= TITLE_SIMILARITY for hit in hits):
        try:
            payload, _ = await get_json(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode(
                    {"query": f'TITLE:"{title}"', "format": "json", "pageSize": "5"}),
                max_bytes=2_000_000)
            for row in (payload.get("resultList") or {}).get("result") or []:
                doi = str(row.get("doi") or "").strip().lower()
                name = row.get("title") or ""
                if doi and name:
                    hits.append({"doi": doi, "title": name, "source": "Europe PMC",
                                 "similarity": title_similarity(title, name)})
        except Exception:
            pass
    hits.sort(key=lambda hit: hit["similarity"], reverse=True)
    return [hit for hit in hits if hit["similarity"] >= TITLE_SIMILARITY]


async def _doi_is_real(doi: str, email: str) -> bool:
    """启发式 DOI 先过一遍 Crossref，免得往论文中心塞不存在的东西。

    网络不通时按「通过」处理：宁可多一条待核实的记录，也不能因为断网漏掉真论文。
    """
    url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
    if email:
        url += "?" + urlencode({"mailto": email})
    try:
        payload, _ = await get_json(url, headers={"Accept": "application/json"}, max_bytes=1_000_000)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status != 404
    return bool((payload.get("message") or {}).get("title"))



def _crossref_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message") or {}
    authors = [" ".join(filter(None, [a.get("given"), a.get("family")])) for a in message.get("author") or []]
    date_parts = (message.get("published-print") or message.get("published-online") or message.get("issued") or {}).get("date-parts") or []
    year = str(date_parts[0][0]) if date_parts and date_parts[0] and date_parts[0][0] else ""
    return {"title": (message.get("title") or [""])[0], "authors": authors, "year": year,
            "journal": (message.get("container-title") or [""])[0], "crossref": message}


def _openalex_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    authors = [((a.get("author") or {}).get("name") or "") for a in payload.get("authorships") or []]
    return {"title": payload.get("title") or "", "authors": authors,
            "year": str(payload.get("publication_year") or ""),
            "journal": ((payload.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
            "openalex": payload}


def _citation_pdf_from_html(html: str) -> str:
    """从落地页的 `<meta name="citation_pdf_url">` 里取 PDF 地址。

    属性顺序各家不一样（name 在前、content 在前都见过），所以先切出 meta 标签
    再判断，不用一条大正则去赌顺序。
    """
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        if "citation_pdf_url" not in tag.lower():
            continue
        found = re.search(r'content\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if found:
            return found.group(1).strip()
    return ""


async def _landing_pdf_url(landing: str) -> str:
    """直接问出版社要 PDF：OpenAlex / Unpaywall 没收的刊也能拿到。

    2026-11-19 的 Vita（10.15302/vita.2026.10.0084）：OpenAlex 返回 404、
    Unpaywall 没有记录、Crossref 只有题录，于是被判成 closed。可它官网页面
    一直挂着 citation_pdf_url。几乎所有 OA 平台都遵循 Highwire 规范，这是比
    元数据库更稳的一条兜底。
    """
    if not landing:
        return ""
    try:
        response = await request(landing, max_bytes=4_000_000,
                                 headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    except Exception:                                    # noqa: BLE001 - 兜底失败不该影响主流程
        return ""
    if response.status >= 400:
        return ""
    return _citation_pdf_from_html(response.text[:800_000])


async def resolve_and_download(db: Database, paper_id: str) -> None:
    paper = db.one("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not paper or paper["status"] == "downloaded":
        return
    settings = get_settings()["papers"]
    doi = paper["doi"]
    email = settings.get("email", "").strip()
    metadata: dict[str, Any] = {}
    errors: list[str] = []
    try:
        crossref_url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
        if email:
            crossref_url += "?" + urlencode({"mailto": email})
        payload, _ = await get_json(crossref_url, headers={"Accept": "application/json"})
        metadata.update(_crossref_metadata(payload))
    except Exception as exc:
        errors.append(f"Crossref：{exc}")
    openalex_payload: dict[str, Any] = {}
    try:
        openalex_url = f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='/')}"
        if email:
            openalex_url += "?" + urlencode({"mailto": email})
        openalex_payload, _ = await get_json(openalex_url)
        oa_meta = _openalex_metadata(openalex_payload)
        for key, value in oa_meta.items():
            if not metadata.get(key):
                metadata[key] = value
    except Exception as exc:
        errors.append(f"OpenAlex：{exc}")
    unpaywall_payload: dict[str, Any] = {}
    if email:
        try:
            unpaywall_url = f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}?{urlencode({'email': email})}"
            unpaywall_payload, _ = await get_json(unpaywall_url)
            if not metadata.get("title"):
                metadata.update(_openalex_metadata(unpaywall_payload) if "authorships" in unpaywall_payload else {
                    "title": unpaywall_payload.get("title") or "", "year": str(unpaywall_payload.get("year") or ""),
                    "journal": unpaywall_payload.get("journal_name") or "",
                    "authors": [a.get("raw_author_name", "") for a in unpaywall_payload.get("z_authors") or []],
                })
        except Exception as exc:
            errors.append(f"Unpaywall：{exc}")
    pdf_candidates: list[str] = []
    landing_candidates: list[str] = []
    oa_status = "unknown"
    if openalex_payload:
        oa_status = (openalex_payload.get("open_access") or {}).get("oa_status") or oa_status
        locations = [openalex_payload.get("best_oa_location") or {}] + (openalex_payload.get("locations") or [])
        for location in locations:
            if location.get("pdf_url"):
                pdf_candidates.append(location["pdf_url"])
            if location.get("landing_page_url"):
                landing_candidates.append(location["landing_page_url"])
    if unpaywall_payload:
        oa_status = unpaywall_payload.get("oa_status") or oa_status
        locations = [unpaywall_payload.get("best_oa_location") or {}] + (unpaywall_payload.get("oa_locations") or [])
        for location in locations:
            if location.get("url_for_pdf"):
                pdf_candidates.append(location["url_for_pdf"])
            if location.get("url_for_landing_page"):
                landing_candidates.append(location["url_for_landing_page"])
    pdf_candidates = list(dict.fromkeys(pdf_candidates))
    landing_candidates = list(dict.fromkeys(landing_candidates))
    if not pdf_candidates:
        # 两个元数据库都没给出 PDF 时，直接问出版社要一次（新刊、国内刊常见）
        probe = landing_candidates[0] if landing_candidates else f"https://doi.org/{doi}"
        fallback = await _landing_pdf_url(probe)
        if fallback:
            pdf_candidates = [fallback]
            landing_candidates = landing_candidates or [probe]
    timestamp = now_iso()
    # matched_by 是入库时记下的「这篇是怎么找到的」，重查一次不能把它抹掉。
    matched_by = (json_loads(paper.get("metadata_json"), {}) or {}).get("matched_by")
    db.execute("""UPDATE papers SET title=?,authors_json=?,year=?,journal=?,oa_status=?,pdf_url=?,landing_url=?,
        metadata_json=?,updated_at=? WHERE id=?""", (
        metadata.get("title") or paper["title"], stable_json(metadata.get("authors") or []),
        metadata.get("year") or paper["year"], metadata.get("journal") or paper["journal"], oa_status,
        pdf_candidates[0] if pdf_candidates else None,
        landing_candidates[0] if landing_candidates else f"https://doi.org/{doi}",
        stable_json({"crossref": metadata.get("crossref"), "openalex": openalex_payload,
                     "unpaywall": unpaywall_payload, "lookup_errors": errors,
                     **({"matched_by": matched_by} if matched_by else {})}), timestamp, paper_id,
    ))
    if not pdf_candidates:
        db.execute("UPDATE papers SET status='closed',error=?,updated_at=? WHERE id=?",
                   ("未发现合法开放 PDF；" + "；".join(errors), timestamp, paper_id))
        return
    max_bytes = int(settings["max_pdf_mb"] * 1024 * 1024)
    download_errors = []
    folder = paper_folder(doi)
    for pdf_url in pdf_candidates[:5]:
        try:
            part_path = folder / "paper.pdf.part"
            if part_path.exists() and part_path.stat().st_size > max_bytes:
                purge.unlink(part_path)
            await download_file(pdf_url, part_path, max_bytes=max_bytes,
                                headers={"Accept": "application/pdf,*/*;q=0.5",
                                         "Referer": landing_candidates[0] if landing_candidates else f"https://doi.org/{doi}"},
                                attempts=6, read_timeout=120)
            local_path = folder / "paper.pdf"
            os.replace(part_path, local_path)
            # 不再往 blobs 里存一份：同一篇 PDF 在 papers/<doi>/ 已经有一份，
            # 再存一遍等于把整库论文翻倍（实测 1493 篇、9.5 GB 纯重复）。
            text_path = None
            if settings.get("extract_text", True):
                reader = await asyncio.to_thread(PdfReader, str(local_path))
                text = await asyncio.to_thread(lambda: "\n\n".join(page.extract_text() or "" for page in reader.pages))
                text_path = atomic_write(folder / "paper.txt", text)
                atomic_write(folder / "metadata.json", json.dumps({
                    "doi": doi, "title": metadata.get("title"), "authors": metadata.get("authors"),
                    "year": metadata.get("year"), "journal": metadata.get("journal"), "pdf_url": pdf_url,
                    "pages": len(reader.pages), "downloaded_at": now_iso(),
                }, ensure_ascii=False, indent=2))
            db.execute("UPDATE papers SET status='downloaded',pdf_url=?,local_path=?,text_path=?,error=NULL,updated_at=? WHERE id=?",
                       (pdf_url, str(local_path), str(text_path) if text_path else None, now_iso(), paper_id))
            return
        except Exception as exc:
            download_errors.append(f"{pdf_url}：{exc}")
    db.execute("UPDATE papers SET status='failed',error=?,updated_at=? WHERE id=?",
               ("；".join(download_errors + errors)[:4000], now_iso(), paper_id))


async def _guess_dois(db: Database, item_id: str, body_text: str, raw_text: str,
                      settings: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """正文没写 DOI 时，从文末链接和引号里的论文标题再找一遍。

    真实文章上的两种写法都见过：文末直接贴 `nature.com/articles/...`（链接里没有
    10.xxxx/，正则认不出），或者正文写「研究报道“Bistable regulation of adipocyte
    heterogeneity governs thermogenesis”」。两种都能反推出 DOI。
    """
    email = str(settings.get("email") or "").strip()
    guessed: list[tuple[str, str]] = [(doi, "正文里贴出的原文链接")
                                      for doi in doi_from_links(body_text + "\n" + raw_text)]
    if not guessed:
        for title in candidate_titles(body_text or raw_text):
            hits = await _title_candidates(title, email)
            if not hits:
                continue
            top = hits[0]
            guessed.append((top["doi"], f"正文标题匹配「{top['title'][:70]}」"
                                        f"（{top['source']}，相似度 {top['similarity']:.2f}）"))
            break
    kept: dict[str, str] = {}
    for doi, reason in guessed[:3]:
        if await _doi_is_real(doi, email):
            kept[doi] = reason
        else:
            db.add_event("paper_lookup", f"按{reason}推出的 DOI {doi} 在 Crossref 查不到，已忽略", item_id)
    return list(kept), kept


async def process_item_papers(db: Database, item_id: str, analysis_after: bool = False,
                              download: bool | None = None) -> None:
    """找论文、下载 PDF。**只做这件事**。

    以前这里顺手按 analysis.auto 排一次精读。结果手动「让软件去找论文」和
    「重试下载」的载荷里没写 skip_analysis，默认 False 就被当成「要精读」——
    用户点找论文，跑的却是云精读（花点数、改条目状态），读成「点了 A 触发 B」。
    现在改成显式开关：只有归档流程明确传 analysis_after=True 才排精读，
    手动找论文 / 重试下载一律不传，结构上不可能再串。
    """
    item = db.one("SELECT id,url,text,raw_path,excluded_at FROM items WHERE id=?", (item_id,))
    if not item:
        raise ValueError("条目不存在")
    if item.get("excluded_at"):
        return
    body_text = item["text"] or ""
    raw_text = ""
    snapshot = raw_file(item["raw_path"])
    if snapshot.is_file() and snapshot.suffix.lower() in {".html", ".htm", ".xml", ".json", ".txt", ".gz"}:
        try:
            data = read_raw_bytes(snapshot)
            # 30 MB 上限按解压后的大小算，避免压缩存储绕过限制
            if len(data) < 30_000_000:
                raw_text = data.decode("utf-8", errors="ignore")
        except (OSError, UnicodeError):
            pass
    primary: list[str] = []
    if raw_text:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_text, "lxml")
        for name in ("citation_doi", "dc.identifier", "dc.identifier.doi", "doi"):
            for tag in soup.find_all("meta", attrs={"name": lambda value: value and value.lower() == name}):
                primary.extend(extract_dois(tag.get("content") or ""))
    primary.extend(extract_dois(item["url"]))
    primary = list(dict.fromkeys(primary))
    candidates = list(dict.fromkeys(primary + extract_dois(body_text) + extract_dois(raw_text)))
    # Figure/supplement DOIs are components of the main work, not separate papers to buy or download.
    candidates = [doi for doi in candidates if not any(doi != parent and doi.startswith(parent + ".") for parent in candidates)]
    settings = get_settings()["papers"]
    if download is None:
        # 老任务（payload 里没这个字段）按开处理，保持升级前行为。
        download = bool(settings.get("auto_download", True))
    if primary and not settings.get("download_references", False):
        candidates = primary
    dois = candidates[:int(settings["max_per_item"])]
    matched: dict[str, str] = {}
    if not dois and settings.get("title_lookup", True):
        dois, matched = await _guess_dois(db, item_id, body_text, raw_text, settings)
    paper_ids = []
    for doi in dois:
        existing = db.one("SELECT id FROM papers WHERE doi=?", (doi,))
        paper_id = existing["id"] if existing else __import__("uuid").uuid4().hex
        timestamp = now_iso()
        with db.transaction() as conn:
            if not existing:
                conn.execute("INSERT INTO papers(id,doi,landing_url,metadata_json,created_at,updated_at) "
                             "VALUES(?,?,?,?,?,?)",
                             (paper_id, doi, f"https://doi.org/{doi}",
                              # 这一列 NOT NULL：没有「怎么找到的」就写空对象，别塞 None。
                              stable_json({"matched_by": matched[doi]}) if doi in matched else "{}",
                              timestamp, timestamp))
            conn.execute("INSERT OR IGNORE INTO item_papers(item_id,paper_id) VALUES(?,?)", (item_id, paper_id))
        paper_ids.append(paper_id)
    if settings["enabled"] and download:
        for paper_id in paper_ids:
            try:
                await resolve_and_download(db, paper_id)
            except Exception as exc:
                db.execute("UPDATE papers SET status='failed',error=?,updated_at=? WHERE id=?",
                           (str(exc)[:4000], now_iso(), paper_id))
    # 归档后要不要顺手精读，只由调用方明确决定（analysis_after）。论文步骤自己不猜。
    if analysis_after:
        db.enqueue_job("analyze_item", {"version": 1, "item_id": item_id},
                       dedupe_key=f"analyze:{item_id}:initial", priority=60, max_attempts=4)
