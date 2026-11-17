import html
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ..net import get_json, request
from ..settings import get_settings
from ..storage import atomic_write, item_directory, store_blob
from . import purge
from ..utils import epoch_iso, extract_bvid, now_iso
from .archive import _clean_text, _reader_html


def _timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


async def _resolve_bvid(url: str) -> str:
    bvid = extract_bvid(url)
    if bvid:
        return bvid
    if "b23.tv/" in url:
        response = await request(url, max_bytes=2_000_000)
        bvid = extract_bvid(response.url) or extract_bvid(response.text)
        if bvid:
            return bvid
    raise ValueError("无法从链接中识别 BV 号")


async def _subtitles(bvid: str, cid: int) -> tuple[str, str, list[dict[str, Any]]]:
    try:
        payload, _ = await get_json(
            f"https://api.bilibili.com/x/player/v2?{urlencode({'bvid': bvid, 'cid': cid})}",
            headers={"Referer": f"https://www.bilibili.com/video/{bvid}/"},
        )
        tracks = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if not tracks:
            return "", "no_subtitles", []
        tracks = sorted(tracks, key=lambda x: (not str(x.get("lan", "")).startswith("zh"), x.get("lan", "")))
        track = tracks[0]
        subtitle_url = track.get("subtitle_url") or ""
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        subtitle_payload, _ = await get_json(subtitle_url)
        body = subtitle_payload.get("body") or []
        lines = [f"[{_timestamp(float(x.get('from', 0)))}] {x.get('content', '').strip()}" for x in body]
        return _clean_text("\n".join(lines)), "platform_subtitles", [{"language": x.get("lan"), "name": x.get("lan_doc")} for x in tracks]
    except Exception:
        return "", "subtitle_fetch_failed", []


async def _local_asr(url: str, folder: Path, duration: int) -> tuple[str, str]:
    settings = get_settings()["video"]
    if not settings["asr_enabled"]:
        return "", "no_subtitles_asr_disabled"
    if duration > int(settings["max_duration_minutes"]) * 60:
        return "", "too_long_for_asr"
    try:
        import yt_dlp
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("已启用本地 ASR，但缺少 yt-dlp 或 faster-whisper") from exc
    output = str(folder / "audio.%(ext)s")
    options = {"format": "bestaudio/best", "outtmpl": output, "noplaylist": True, "quiet": True, "no_warnings": True}
    await __import__("asyncio").to_thread(lambda: yt_dlp.YoutubeDL(options).download([url]))
    audio_files = [p for p in folder.glob("audio.*") if p.is_file()]
    if not audio_files:
        raise RuntimeError("未找到下载的音频")
    def transcribe() -> list[Any]:
        model = WhisperModel(settings["asr_model"], device=settings["asr_device"], compute_type="int8")
        segments, _ = model.transcribe(str(audio_files[0]), vad_filter=True)
        return list(segments)
    segment_list = await __import__("asyncio").to_thread(transcribe)
    text = "\n".join(f"[{_timestamp(s.start)}] {s.text.strip()}" for s in segment_list)
    if not settings["keep_audio"]:
        purge.unlink(audio_files[0])
    return _clean_text(text), "local_faster_whisper"


async def archive_bilibili(item: dict[str, Any]) -> dict[str, Any]:
    folder = item_directory(item["id"])
    bvid = await _resolve_bvid(item["url"])
    payload, _ = await get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                                headers={"Referer": f"https://www.bilibili.com/video/{bvid}/"})
    if payload.get("code") != 0:
        raise RuntimeError(f"B站视频信息接口返回 {payload.get('code')}：{payload.get('message')}")
    data = payload["data"]
    cid = int(data["cid"])
    title = data.get("title") or bvid
    author = (data.get("owner") or {}).get("name") or ""
    description = _clean_text(data.get("desc") or "")
    transcript, transcript_status, tracks = await _subtitles(bvid, cid)
    asr_error = ""
    if not transcript:
        try:
            transcript, asr_status = await _local_asr(item["url"], folder, int(data.get("duration") or 0))
            transcript_status = asr_status or transcript_status
        except Exception as exc:
            transcript_status = "asr_failed"
            asr_error = str(exc)
    evidence = "full_transcript" if transcript else "metadata_and_description_only"
    text = _clean_text(f"{title}\nUP主：{author}\n\n视频简介：\n{description}\n\n字幕/转录：\n{transcript}")
    atomic_write(folder / "content.txt", text)
    if transcript:
        atomic_write(folder / "transcript.txt", transcript)
    raw_path = atomic_write(folder / "bilibili-view.json", json.dumps(payload, ensure_ascii=False, indent=2))
    cover_meta: dict[str, Any] = {}
    if data.get("pic"):
        try:
            cover = await request(data["pic"], max_bytes=10_000_000)
            _, cover_path = store_blob(cover.content, ".jpg")
            cover_meta = {"local_path": str(cover_path), "relative_path": f"../../blobs/{cover_path.parent.name}/{cover_path.name}"}
        except Exception as exc:
            cover_meta = {"error": str(exc)}
    cover_html = f'<img src="{cover_meta["relative_path"]}" alt="视频封面">' if cover_meta.get("relative_path") else ""
    warning = "" if transcript else "本视频没有可用字幕，当前仅依据标题和简介归档；未声称理解视频画面或完整口播。"
    if asr_error:
        warning += " 本地转录失败：" + asr_error
    body = (cover_html + f'<p><b>UP主：</b>{html.escape(author)}</p><p><b>简介</b></p>'
            f'<pre>{html.escape(description)}</pre><p><b>字幕/转录</b></p><pre>{html.escape(transcript or "暂无")}</pre>')
    html_path = atomic_write(folder / "reader.html", _reader_html(title, author, item["url"], body, warning))
    metadata = {
        "bvid": bvid, "cid": cid, "duration_seconds": data.get("duration"), "pages": data.get("pages") or [],
        "statistics": data.get("stat") or {}, "cover": cover_meta, "subtitle_tracks": tracks,
        "transcript_status": transcript_status, "archive_completeness": evidence, "archived_at": now_iso(),
        "warnings": [warning] if warning else [],
    }
    atomic_write(folder / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return {"title": title, "author": author, "published_at": epoch_iso(data.get("pubdate")), "text": text,
            "html_path": str(html_path), "raw_path": str(raw_path), "metadata": metadata}
