import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from pypdf import PdfReader

from ..db import Database
from ..net import download_file, get_json
from ..settings import get_settings
from ..storage import atomic_write, paper_folder, store_blob_file
from ..utils import extract_dois, json_loads, now_iso, stable_json


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
    timestamp = now_iso()
    db.execute("""UPDATE papers SET title=?,authors_json=?,year=?,journal=?,oa_status=?,pdf_url=?,landing_url=?,
        metadata_json=?,updated_at=? WHERE id=?""", (
        metadata.get("title") or paper["title"], stable_json(metadata.get("authors") or []),
        metadata.get("year") or paper["year"], metadata.get("journal") or paper["journal"], oa_status,
        pdf_candidates[0] if pdf_candidates else None,
        landing_candidates[0] if landing_candidates else f"https://doi.org/{doi}",
        stable_json({"crossref": metadata.get("crossref"), "openalex": openalex_payload,
                     "unpaywall": unpaywall_payload, "lookup_errors": errors}), timestamp, paper_id,
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
                part_path.unlink()
            await download_file(pdf_url, part_path, max_bytes=max_bytes,
                                headers={"Accept": "application/pdf,*/*;q=0.5",
                                         "Referer": landing_candidates[0] if landing_candidates else f"https://doi.org/{doi}"},
                                attempts=6, read_timeout=120)
            local_path = folder / "paper.pdf"
            os.replace(part_path, local_path)
            store_blob_file(local_path, ".pdf")
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


async def process_item_papers(db: Database, item_id: str, archive_only: bool = False,
                              skip_analysis: bool = False) -> None:
    item = db.one("SELECT id,url,text,raw_path,excluded_at FROM items WHERE id=?", (item_id,))
    if not item:
        raise ValueError("条目不存在")
    if item.get("excluded_at"):
        return
    body_text = item["text"] or ""
    raw_text = ""
    raw_path = Path(item["raw_path"] or "")
    if raw_path.suffix.lower() in {".html", ".htm", ".xml", ".json", ".txt"} and raw_path.is_file() and raw_path.stat().st_size < 30_000_000:
        try:
            raw_text = raw_path.read_text(encoding="utf-8", errors="ignore")
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
    if primary and not settings.get("download_references", False):
        candidates = primary
    dois = candidates[:int(settings["max_per_item"])]
    paper_ids = []
    for doi in dois:
        existing = db.one("SELECT id FROM papers WHERE doi=?", (doi,))
        paper_id = existing["id"] if existing else __import__("uuid").uuid4().hex
        timestamp = now_iso()
        with db.transaction() as conn:
            if not existing:
                conn.execute("INSERT INTO papers(id,doi,landing_url,created_at,updated_at) VALUES(?,?,?,?,?)",
                             (paper_id, doi, f"https://doi.org/{doi}", timestamp, timestamp))
            conn.execute("INSERT OR IGNORE INTO item_papers(item_id,paper_id) VALUES(?,?)", (item_id, paper_id))
        paper_ids.append(paper_id)
    if get_settings()["papers"]["enabled"]:
        for paper_id in paper_ids:
            try:
                await resolve_and_download(db, paper_id)
            except Exception as exc:
                db.execute("UPDATE papers SET status='failed',error=?,updated_at=? WHERE id=?",
                           (str(exc)[:4000], now_iso(), paper_id))
    if not archive_only and not skip_analysis:
        db.enqueue_job("analyze_item", {"version": 1, "item_id": item_id},
                       dedupe_key=f"analyze:{item_id}:initial", priority=60, max_attempts=4)
