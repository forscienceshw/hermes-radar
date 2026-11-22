import asyncio
import base64
import gzip
import html
import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..net import request
from ..settings import get_settings
from ..storage import atomic_write, item_directory, store_blob
from . import purge
from ..utils import clean_image_alt, now_iso, safe_name


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


# ---- 原始快照读取 ----
# raw.html 可能被 gzip 压缩成 raw.html.gz 以节省空间。
# 所有读取 raw 的地方都走这几个函数，不要直接 open(raw_path)。

def raw_file(path: Path | str | None) -> Path:
    """返回实际存在的快照文件：优先原文件，其次 .gz 压缩版。"""
    p = Path(path or "")
    if p.is_file():
        return p
    gz = Path(str(p) + ".gz")
    if gz.is_file():
        return gz
    return p


def read_raw_bytes(path: Path | str | None) -> bytes:
    p = raw_file(path)
    if p.suffix.lower() == ".gz":
        with gzip.open(p, "rb") as fh:
            return fh.read()
    return p.read_bytes()


def read_raw_text(path: Path | str | None) -> str:
    """按 utf-8 容错解码，压缩与否都能读。"""
    try:
        return read_raw_bytes(path).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _clean_text(value: str) -> str:
    value = value.replace("\r", "\n").replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n\s*\n+", "\n\n", value)
    return value.strip()


def _image_suffix(content_type: str, url: str) -> str:
    mapping = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif"}
    if content_type.split(";", 1)[0].lower() in mapping:
        return mapping[content_type.split(";", 1)[0].lower()]
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"} else ".img"


# Page furniture: QR codes, avatars, logos, "follow us" strips and ad slots.  These
# were the bulk of the 1.2 GB blob folder and none of them is worth keeping.
JUNK_ATTR_RE = re.compile(
    r"(qr|qrcode|qr_code|avatar|logo|icon|emoji|emoticon|reward|sponsor|advert|\bad\b|"
    r"banner|footer|header|profile|copyright|share_|follow|watermark|guide|badge)", re.I)
JUNK_TEXT_RE = re.compile(r"(二维码|扫码|扫一扫|关注公众号|长按识另|广告|推广|赞赏|打赏|头像|名片|公众号名片|"
                          r"在看|点赞|置顶|投稿|招聘|征稿|课程|社群)", re.I)
JUNK_URL_RE = re.compile(r"(qrcode|qr_code|/qr/|avatar|logo|/icon/|blank\.gif|pic_blank|emotion|emoji)", re.I)


def junk_image_reason(tag: Any, image_url: str) -> str:
    """Cheap, offline triage for images that are obviously not part of the article."""
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    attrs = " ".join([*[str(item) for item in classes], str(tag.get("id") or ""),
                      str(tag.get("alt") or ""), str(tag.get("data-type") or "")])
    match = JUNK_ATTR_RE.search(attrs)
    if match:
        return f"页面元素（{match.group(1)}）"
    alt = str(tag.get("alt") or "")
    if alt and JUNK_TEXT_RE.search(alt):
        return "二维码或引导关注类图片"
    if JUNK_URL_RE.search(image_url):
        return "站点图标或占位图"
    try:
        width = float(str(tag.get("data-w") or 0))
    except ValueError:
        width = 0
    # WeChat records the natural width; emoji and inline icons sit far below body width.
    if 0 < width < 160:
        return f"过小的图标（宽 {int(width)}px）"
    return ""


def tiny_image_reason(data: bytes) -> str:
    """Reject spacers, emoji and blank placeholders before they reach the blob store."""
    try:
        from PIL import Image, ImageStat
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            deviation = sum(ImageStat.Stat(image.convert("RGB")).stddev) / 3
    except Exception:
        return ""
    if min(width, height) < 110:
        return f"尺寸过小（{width}×{height}）"
    if len(data) < 3500 and width * height < 60000:
        return f"近似空白图（{width}×{height}）"
    # WeChat ships full-page white placeholders when a lazy image never loads;
    # they weigh hundreds of KB each and show nothing.
    if deviation < 1.5:
        return f"纯色空白图（{width}×{height}）"
    return ""



async def _download_image(url: str, index: int, max_bytes: int) -> dict[str, Any]:
    # Measured on six articles: WeChat's `/640` path is a quality tier, not a width
    # clamp — it returns the 693-840 px bitmap the publisher uploaded, identical in
    # size to `/0`.  Asking for `/0` would only add a round trip, so take what the
    # page references.  Blurriness here comes from the upload, not from us.
    try:
        response = await request(url, max_bytes=max_bytes,
                                 headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        content_type = response.headers.get("content-type", "")
        if content_type and not content_type.lower().startswith("image/"):
            raise ValueError(f"返回类型不是图片：{content_type}")
        reason = tiny_image_reason(response.content)
        if reason:
            # Nothing is written to disk: the blob folder is content-addressed and
            # every saved byte here used to be a spacer or an emoji.
            return {"index": index, "url": url, "skipped": reason}
        digest, path = store_blob(response.content, _image_suffix(content_type, response.url))
        return {"index": index, "url": url, "sha256": digest, "local_path": str(path),
                "relative_path": f"../../blobs/{path.parent.name}/{path.name}", "bytes": len(response.content)}
    except Exception as exc:
        return {"index": index, "url": url, "error": str(exc)}


def _image_alt(tags: list[Any]) -> str:
    """Return a usable caption carried by the <img> itself, or ''."""
    for tag in tags:
        cleaned = clean_image_alt(str(tag.get("alt") or tag.get("title") or ""))
        if cleaned:
            return cleaned
    return ""


def _sanitize(root: BeautifulSoup) -> None:
    for tag in root.select("script,style,iframe,form,input,button,noscript,svg,canvas,object,embed,meta,base,link"):
        tag.decompose()
    # The root element itself must be cleaned too: WeChat ships
    # <div id="js_content" style="visibility:hidden;opacity:0"> and the old loop
    # only visited children, so the archived reader showed a title and nothing else.
    tags = list(root.find_all(True))
    if getattr(root, "attrs", None) is not None:
        tags.insert(0, root)
    for tag in tags:
        attrs = dict(tag.attrs)
        for attr in attrs:
            if attr not in {"href", "src", "alt", "title", "colspan", "rowspan"}:
                del tag.attrs[attr]
        src = tag.get("src", "")
        if src and not src.startswith(("http://", "https://", "../../blobs/")):
            del tag.attrs["src"]
        if tag.name == "a":
            href = tag.get("href", "")
            if href and not href.startswith(("http://", "https://")):
                del tag.attrs["href"]
            else:
                tag.attrs["target"] = "_blank"
                tag.attrs["rel"] = "noopener nofollow"


def _reader_html(title: str, author: str, source_url: str, body: str, warning: str = "") -> str:
    notice = f'<div class="notice">{html.escape(warning)}</div>' if warning else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{max-width:820px;margin:36px auto;padding:0 20px;font:17px/1.85 system-ui,'Microsoft YaHei',sans-serif;color:#20242a;background:#fff}}h1{{line-height:1.35;font-size:30px}}.meta{{color:#68707b;font-size:14px;border-bottom:1px solid #e5e7eb;padding-bottom:18px}}img{{max-width:100%;height:auto;cursor:zoom-in}}a{{color:#1769aa}}.notice{{background:#fff5dc;border:1px solid #edd69b;padding:12px;border-radius:8px}}pre{{white-space:pre-wrap}}#js_content,[style*="visibility"],[style*="opacity"]{{visibility:visible!important;opacity:1!important}}#imageViewer{{position:fixed;inset:0;z-index:99;background:rgba(10,16,22,.95);overflow:hidden;cursor:grab}}#imageViewer[hidden]{{display:none}}#imageViewer img{{position:absolute;transform-origin:0 0;max-width:none;max-height:none;background:#fff;box-shadow:0 12px 48px rgba(0,0,0,.55);cursor:zoom-out}}.imgdim{{font-size:12px;color:#8a929c;margin:-4px 0 14px;font-family:ui-monospace,Consolas,monospace}}#viewerHint{{position:fixed;left:0;right:0;bottom:14px;text-align:center;color:#c6d2dc;font-size:13px;pointer-events:none}}</style></head>
<body><h1>{html.escape(title)}</h1><div class="meta">{html.escape(author)} · <a href="{html.escape(source_url, quote=True)}">原始页面</a></div>{notice}<article>{body}</article>
<div id="imageViewer" hidden><img id="viewerImg" alt=""><div id="viewerHint">双击放大 / 复位 · 滚轮缩放 · 拖动平移 · Esc 关闭</div></div>
<script>
(function(){{
  var box=document.getElementById('imageViewer'),im=document.getElementById('viewerImg'),hint=document.getElementById('viewerHint');
  var s=1,x=0,y=0,fit=1,drag=false,moved=false,sx=0,sy=0;
  function apply(){{im.style.transform='translate('+x+'px,'+y+'px) scale('+s+')';hint.textContent=Math.round(s*100)+'% · 双击复位 · 滚轮缩放 · 拖动平移 · Esc 关闭'}}
  function reset(){{var sw=innerWidth,sh=innerHeight,iw=im.naturalWidth||sw,ih=im.naturalHeight||sh;fit=Math.min(1,sw/iw,sh/ih)||1;s=fit;x=(sw-iw*s)/2;y=(sh-ih*s)/2;apply()}}
  function open(src,alt){{im.src=src;im.alt=alt||'';box.hidden=false;document.body.style.overflow='hidden';if(im.complete)reset();else im.onload=reset}}
  function close(){{box.hidden=true;document.body.style.overflow='';im.removeAttribute('src')}}
  function zoom(f,cx,cy){{var sw=innerWidth,sh=innerHeight;cx=(cx==null)?sw/2:cx;cy=(cy==null)?sh/2:cy;var n=Math.min(8,Math.max(.05,s*f)),r=n/s;x=cx-(cx-x)*r;y=cy-(cy-y)*r;s=n;apply()}}
  document.addEventListener('dblclick',function(e){{
    if(e.target===im){{e.preventDefault();if(s<=fit*1.02)zoom(Math.max(2,fit>0?1/fit:2),e.clientX,e.clientY);else reset();return}}
    if(e.target.tagName!=='IMG')return;e.preventDefault();open(e.target.getAttribute('src'),e.target.alt);
  }});
  box.addEventListener('wheel',function(e){{if(box.hidden)return;e.preventDefault();zoom(Math.exp(-e.deltaY*0.0015),e.clientX,e.clientY);}},{{passive:false}});
  im.addEventListener('mousedown',function(e){{drag=true;moved=false;sx=e.clientX-x;sy=e.clientY-y;e.preventDefault();}});
  window.addEventListener('mousemove',function(e){{if(!drag)return;moved=true;x=e.clientX-sx;y=e.clientY-sy;apply();}});
  window.addEventListener('mouseup',function(){{drag=false;}});
  box.addEventListener('click',function(e){{if(e.target===box&&!moved)close();}});
  document.addEventListener('keydown',function(e){{if(e.key==='Escape'&&!box.hidden)close();}});
  // Show the real pixel size under every figure.  A 674 px wide screenshot cannot
  // be made sharper by zooming; saying so beats letting it look like our compression.
  Array.prototype.forEach.call(document.querySelectorAll('article img'),function(img){{
    function tag(){{
      if(!img.naturalWidth)return;
      var nx=img.nextElementSibling;
      if(nx&&nx.className==='imgdim'){{nx.textContent=label();return;}}
      var d=document.createElement('div');d.className='imgdim';d.textContent=label();
      img.insertAdjacentElement('afterend',d);
    }}
    function label(){{
      var w=img.naturalWidth,h=img.naturalHeight;
      return '原图 '+w+'×'+h+'px'+(w<900?'（发布方就是这个尺寸，放大到超过 100% 只会变糊）':'')+' · 双击放大';
    }}
    if(img.complete)tag();else img.addEventListener('load',tag);
  }});
}})();
</script>
</body></html>"""


WECHAT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://mp.weixin.qq.com/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Pages WeChat serves instead of the article once the publisher removes it.
GONE_MARKERS = (
    ("该内容已被发布者删除", "发布者已删除这篇推文"),
    ("此内容因违规无法查看", "内容因违规被平台屏蔽"),
    ("此内容被多人投诉", "内容被多人投诉，已无法查看"),
    ("该内容暂时无法查看", "内容暂时无法查看"),
    ("该公众号已迁移", "公众号已迁移，原链接失效"),
    ("该账号已注销", "发布账号已注销"),
    ("链接已过期", "链接已过期"),
    ("内容已删除", "内容已删除"),
)


class SourceGone(RuntimeError):
    """The publisher removed the page; whether it stays depends on the local archive."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class WechatNeedsVerification(RuntimeError):
    """微信把直接抓取挡在了人机验证页后面。

    这不是「这篇文章坏了」——多半是本机这一刻被风控限流。必须当成「稍后重试」，
    而不是把任务记成永久失败，否则一篇真文章会被永久判死。
    """

    retry_after_minutes = 45


class NotAnArticle(RuntimeError):
    """链接指向的不是文章正文（图片分享页 / 视频页 / 小程序页）。

    这类端点本身就不返回文章 HTML，重试多少次都一样，所以入口直接判定，
    不走网络、不占重试预算，并给一个准确理由。
    """


def _page_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return text
    return response.content.decode("utf-8", errors="replace")


def detect_gone(root: BeautifulSoup | None, page_text: str) -> str:
    """Return a human reason when the page is a deletion notice, else ''."""
    if root is not None:
        return ""
    for marker, reason in GONE_MARKERS:
        if marker in page_text:
            return reason
    return ""


# 微信对「图片消息」（小绿书）和可疑请求一律换成这个验证页壳子：
# 页面只有 17 KB、没有 #js_content，正文在 secitptpage 的验证脚本里，抓不到。
VERIFY_MARKERS = ("secitptpage/template/verify.js", "TCaptcha.js", "cap_sid")


def detect_verify_page(page_text: str) -> str:
    """识别微信的人机验证页，返回给人看的原因；不是验证页时返回空串。"""
    if not page_text:
        return ""
    hits = sum(1 for marker in VERIFY_MARKERS if marker in page_text)
    if hits < 2:
        return ""
    if "image_detail" in page_text:
        return "这是公众号的图片消息，微信对外只返回人机验证页，拿不到正文和原图"
    return "微信返回人机验证页，这一次没拿到正文"


async def probe_source(url: str) -> dict[str, Any]:
    """Check whether the original page still exists, without writing anything."""
    headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
    if "mp.weixin.qq.com" in url:
        headers.update(WECHAT_HEADERS)
    try:
        response = await request(url, max_bytes=4 * 1024 * 1024, headers=headers)
    except Exception as exc:
        text = str(exc)
        if "404" in text or "410" in text or "Not Found" in text:
            return {"exists": False, "reason": "原页面已返回 404，内容不存在"}
        raise RuntimeError(f"检查原文失败：{exc}") from None
    if "mp.weixin.qq.com" in url:
        soup = BeautifulSoup(response.content, "lxml")
        if soup.select_one("#js_content") is None:
            reason = detect_gone(None, _page_text(response))
            if reason:
                return {"exists": False, "reason": reason}
            return {"exists": True, "reason": "微信返回验证页，暂时无法确认原文状态"}
    return {"exists": True, "reason": ""}



def _wechat_video(html_text: str, soup: BeautifulSoup) -> dict[str, Any]:
    """Pull the video id, cover and mp4 sources out of a WeChat video article.

    A 视频号 article ships an empty `#js_content` shell; everything real lives in
    the inline `cgiData` blob.  The article is still worth keeping — the title,
    the cover and a playable link are the whole content of the post.
    """
    sources: list[dict[str, Any]] = []
    sizes = [int(x) for x in re.findall(r"filesize:\s*'(\d+)'", html_text)]
    urls = re.findall(r"url:\s*'([^']*mpvideo\.qpic\.cn[^']*?\.mp4[^']*)'", html_text)
    for index, raw in enumerate(dict.fromkeys(urls)):
        url = html.unescape(raw).replace("\\x26", "&")
        sources.append({"url": url, "bytes": sizes[index] if index < len(sizes) else 0,
                        "label": (re.search(r"\.(f\d+)\.mp4", url) or [None, ""])[1]})
    video_id = (re.search(r"video_id:\s*'(wxv_[0-9A-Za-z]+)'", html_text)
                or re.search(r"wxv_[0-9A-Za-z]+", html_text))
    if not sources and not video_id:
        return {}
    cover = _meta_content(soup, "og:image", "twitter:image")
    duration = (re.search(r"duration:\s*'?(\d+)'?", html_text) or [None, ""])[1]
    return {"video_id": video_id.group(1) if video_id and video_id.lastindex else (video_id.group(0) if video_id else ""),
            "cover": cover, "sources": sources, "duration_seconds": int(duration) if str(duration).isdigit() else None,
            "bytes": max((s["bytes"] for s in sources), default=0)}


async def _archive_wechat_video(item: dict[str, Any], folder: Path, title: str, author: str,
                                video: dict[str, Any], final_url: str) -> dict[str, Any]:
    """Keep the cover and the playable link; never mirror a 60 MB mp4."""
    settings = get_settings()
    images: list[dict[str, Any]] = []
    cover = video.get("cover") or ""
    if cover:
        downloaded = await _download_image(cover, 0, int(settings["max_image_mb"] * 1024 * 1024))
        if downloaded.get("local_path"):
            images.append(downloaded)
    # Play the lightest rendition: the link is short-lived anyway and a 58 MB
    # first frame is a worse experience than a 8 MB one on the same article.
    smallest = min((s for s in video.get("sources") or [] if s.get("bytes")), key=lambda s: s["bytes"], default=None)
    playback = (smallest or {}).get("url") or ""
    figure = ""
    if images:
        figure += f'<p><img src="{html.escape(images[0]["relative_path"], quote=True)}" alt="视频封面"></p>'
    if playback:
        # The signed mp4 link expires; the 原始页面 link above always works.
        figure += (f'<video controls preload="none" poster="'
                   f'{html.escape(images[0]["relative_path"], quote=True) if images else ""}" '
                   f'src="{html.escape(playback, quote=True)}" style="max-width:100%"></video>')
    size_text = ""
    if video.get("bytes"):
        size_text = f"，约 {video['bytes'] / 1024 / 1024:.0f} MB"
    body = (figure + f'<p>这是一条公众号视频（视频号 {html.escape(video.get("video_id") or "")}）'
            f'{size_text}。视频文件没有下载到本地，上面是原站直链，'
            f'链接过期时请点顶部的「原始页面」在微信里观看。</p>')
    html_path = atomic_write(folder / "reader.html", _reader_html(title, author, final_url, body))
    text = f"公众号视频：{title}"
    atomic_write(folder / "content.txt", text)
    metadata = {"images": images, "media_kind": "video",
                "video": {k: v for k, v in video.items() if k != "cover"},
                "archive_completeness": "video", "archived_at": now_iso(), "final_url": final_url,
                "warnings": [f"视频未下载，直链约 {size_text.lstrip('，') or '未知大小'}"]}
    atomic_write(folder / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return {"title": title, "author": author, "text": text, "html_path": str(html_path),
            "raw_path": None, "metadata": metadata}


def _share_card_archive(item: dict[str, Any], folder: Path, url: str, reason: str,
                        final_url: str = "") -> dict[str, Any]:
    """微信不给正文时，用分享卡片上的文字与封面拼一个能读的离线页。

    图片消息（小绿书）的正文就是图和一段说明，微信对外只发验证页；
    分享卡片里的 `description` 恰好就是那段说明，把它留下比记一条 error 有用：
    用户至少能看到这篇在讲什么，也能一键跳回微信。
    """
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    title = str(item.get("title") or "").strip() or "未命名"
    description = str(metadata.get("description") or "").strip()
    author = str(item.get("author") or "").strip()
    if len(description) < 20:
        raise ValueError(f"{reason}；分享卡片上也只有「{description[:16]}」这么短的文字，"
                         f"没有可归档的内容——这条只能回微信里看")

    paragraphs = [line.strip() for line in description.splitlines() if line.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [description]
    body = "".join(f"<p>{html.escape(line)}</p>" for line in paragraphs)
    body += (f'<p class="meta">原文在这里：<a href="{html.escape(url, quote=True)}">'
             f'在微信里打开</a></p>')
    warning = f"{reason}。下面这段是分享卡片上的文字，不是抓到的正文；点上面的链接回微信看原图。"
    html_path = atomic_write(folder / "reader.html", _reader_html(title, author, url, body, warning))
    atomic_write(folder / "content.txt", description)
    atomic_write(folder / "content.md", f"# {title}\n\n{description}\n\n原文：{url}\n")
    result_metadata = {
        "images": [],
        "archive_completeness": "share_card",
        "degraded_reason": reason,
        "final_url": final_url or url,
        "archived_at": now_iso(),
    }
    atomic_write(folder / "metadata.json", json.dumps(result_metadata, ensure_ascii=False, indent=2))
    return {"title": title, "author": author, "published_at": None, "text": description,
            "html_path": str(html_path), "raw_path": None, "metadata": result_metadata}


async def archive_web(item: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    folder = item_directory(item["id"])
    url = item["url"]
    if "mp.weixin.qq.com" in url and "t=pages/" in url:
        # 这不是文章正文页（分享图片/视频的浏览器页），端点本身就不返回 HTML，
        # 重试多少次都一样；直接用卡片文字拼离线页，一次网络请求都不发。
        return _share_card_archive(item, folder, url,
                                   "这是公众号的图片/视频分享页（链接里带 t=pages/），"
                                   "不是文章正文页，微信不对外返回内容")
    headers = {"Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5"}
    if "mp.weixin.qq.com" in url:
        # WeChat serves a 18 KB "environment abnormal" interstitial to unknown agents.
        headers.update(WECHAT_HEADERS)
    try:
        response = await request(url, max_bytes=int(settings["max_html_mb"] * 1024 * 1024), headers=headers)
    except Exception as exc:
        text = str(exc)
        if "404" in text or "410" in text or "Not Found" in text:
            raise SourceGone("原页面已返回 404，内容不存在") from None
        raise RuntimeError(f"直接抓取原文失败：{exc}") from None
    if response.content.startswith(b"%PDF-") or "application/pdf" in response.headers.get("content-type", ""):
        _, pdf_path = store_blob(response.content, ".pdf")
        from pypdf import PdfReader
        reader_pdf = PdfReader(str(pdf_path))
        text = _clean_text("\n\n".join(page.extract_text() or "" for page in reader_pdf.pages))
        title = item.get("title") or safe_name(Path(urlsplit(url).path).stem)
        body = f'<p>已归档 PDF，共 {len(reader_pdf.pages)} 页。</p><p><a href="{html.escape(url)}">打开原始 PDF</a></p>'
        html_path = atomic_write(folder / "reader.html", _reader_html(title, "", url, body))
        atomic_write(folder / "content.txt", text)
        return {"title": title, "author": "", "text": text, "html_path": str(html_path), "raw_path": str(pdf_path),
                "metadata": {"images": [], "archive_completeness": "pdf", "archived_at": now_iso()}}
    soup = BeautifulSoup(response.content, "lxml")
    title = _meta_content(soup, "og:title", "twitter:title") or (soup.title.get_text(" ", strip=True) if soup.title else "") or item.get("title") or "未命名"
    hostname = urlsplit(response.url).hostname or ""
    wechat = hostname.endswith("mp.weixin.qq.com")
    content_root = soup.select_one("#js_content") if wechat else None
    if wechat and content_root is None:
        page_text = _page_text(response)
        gone = detect_gone(None, page_text)
        if gone:
            raise SourceGone(gone)
        verify = detect_verify_page(page_text)
        if verify:
            if "image_detail" in page_text:
                # 图片消息（小绿书）本来就没有正文 HTML，重试也没用。
                return _share_card_archive(item, folder, url, verify, response.url)
            # 普通文章的验证页 = 这一刻被风控限流。文章还在，过一会儿再来。
            raise WechatNeedsVerification(
                "微信返回了人机验证页（本机这一刻被风控限流），正文还没拿到；"
                f"{WechatNeedsVerification.retry_after_minutes} 分钟后会自动重试")
        raise ValueError("微信返回的页面里没有正文，也不是验证页；本次只重试直接抓取，不会调用付费接口")
    root = content_root
    root = root or soup.find("article") or soup.find("main") or soup.body or soup
    author = _meta_content(soup, "author", "article:author")
    if wechat:
        name_tag = soup.select_one("#js_name")
        author = (name_tag.get_text(" ", strip=True) if name_tag else "") or author
    published = _meta_content(soup, "article:published_time", "publishdate", "pubdate") or None
    if wechat and content_root is not None and "share_notice_wrp" in (content_root.get("class") or []):
        # A 视频号 post: `#js_content` is an empty share shell, the real payload is
        # the inline cgiData blob (wxv_ id plus expiring mp4 links).
        video = _wechat_video(_page_text(response), soup)
        if video:
            return await _archive_wechat_video(item, folder, title, author, video, response.url)
    text = _clean_text(root.get_text("\n", strip=True))
    if len(text) < 200 and not wechat:
        import trafilatura
        extracted = trafilatura.extract(response.text, include_comments=False, include_tables=True, favor_recall=True)
        if extracted and len(extracted) > len(text):
            text = _clean_text(extracted)

    url_tags: dict[str, list[Any]] = {}
    skipped_images: list[dict[str, str]] = []
    for tag in root.find_all("img"):
        source = tag.get("data-src") or tag.get("data-original") or tag.get("src")
        if not source or source.startswith("data:"):
            continue
        image_url = urljoin(response.url, source)
        reason = junk_image_reason(tag, image_url)
        if reason:
            # Drop the node now: leaving an <img> with no src only produces a broken
            # placeholder in the offline reader.
            skipped_images.append({"url": image_url, "reason": reason})
            tag.decompose()
            continue
        url_tags.setdefault(image_url, []).append(tag)
    if len(text) < 20 and not url_tags:
        # An illustrated post can be nothing but pictures — a two-frame anatomy
        # plate has no prose at all and is still worth keeping.  Only a page with
        # neither prose nor figures is a genuine failure.
        if wechat:
            # 微信有时给一个空的 #js_content 空壳（正文在客户端里渲染）。空壳不是
            # 永久失败：先看是不是验证页，是就稍后重试；只有卡片文字实在没内容时才降级。
            verify = detect_verify_page(_page_text(response))
            if verify and "image_detail" not in _page_text(response):
                raise WechatNeedsVerification(
                    "微信返回了人机验证页（本机这一刻被风控限流），正文还没拿到；"
                    f"{WechatNeedsVerification.retry_after_minutes} 分钟后会自动重试")
            return _share_card_archive(item, folder, url,
                                       verify or "微信只返回了空壳页面，正文没取到", response.url)
        raise ValueError("没有提取到有效正文，页面上也没有可归档的配图；已保留任务以便重试，"
                         "不会伪造摘要或调用付费接口")
    unique_images = list(url_tags.items())[:int(settings["max_images_per_article"])]
    semaphore = asyncio.Semaphore(4)

    async def bounded(url_image: str, index: int) -> dict[str, Any]:
        async with semaphore:
            return await _download_image(url_image, index, int(settings["max_image_mb"] * 1024 * 1024))

    images = await asyncio.gather(*(bounded(image_url, index) for index, (image_url, tags) in enumerate(unique_images)))
    for (image_url, tags), image in zip(unique_images, images):
        if image.get("skipped"):
            for tag in tags:
                tag.decompose()
            continue
        # WeChat sometimes carries the whole caption in the img alt text; keep it so
        # the figure grid still has something to show when the body has no 图N paragraph.
        alt = _image_alt(tags)
        if alt:
            image["alt"] = alt
        for tag in tags:
            if image.get("relative_path"):
                tag["src"] = image["relative_path"]
            else:
                tag["data-archive-error"] = image.get("error", "下载失败")[:180]
            for attr in ("data-src", "data-original", "srcset", "data-srcset", "loading"):
                if attr in tag.attrs:
                    del tag[attr]
    skipped_images.extend({"url": image["url"], "reason": image["skipped"]}
                          for image in images if image.get("skipped"))
    _sanitize(root)
    raw_path = atomic_write(folder / "raw.html", response.content)
    html_path = atomic_write(folder / "reader.html", _reader_html(title, author, response.url, str(root)))
    atomic_write(folder / "content.txt", text)
    markdown = f"# {title}\n\n作者/来源：{author}\n\n原始链接：{response.url}\n\n---\n\n{text}\n"
    atomic_write(folder / "content.md", markdown)
    kept_images = [image for image in images if image.get("local_path") or image.get("error")]
    failed = [image for image in kept_images if image.get("error")]
    if not text.strip() and kept_images and not failed:
        completeness = "images_only"
    elif kept_images and not failed:
        completeness = "full_text_and_images"
    elif kept_images:
        completeness = "full_text_partial_images"
    else:
        completeness = "full_text"
    metadata = {"images": kept_images, "warnings": [f"{len(failed)} 张图片下载失败"] if failed else [],
                "archive_completeness": completeness, "archived_at": now_iso(), "final_url": response.url}
    if skipped_images:
        metadata["skipped_images"] = skipped_images[:40]
    atomic_write(folder / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return {"title": title, "author": author, "published_at": published, "text": text,
            "html_path": str(html_path), "raw_path": str(raw_path), "metadata": metadata}


async def archive_item(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("kind") == "video" or item.get("metadata", {}).get("bvid"):
        from .video import archive_bilibili
        return await archive_bilibili(item)
    return await archive_web(item)


def drop_archived_images(db: Any, item_id: str, indexes: list[int], note: str = "") -> int:
    """Remove images the vision pass judged to be ads, contact cards or decoration.

    The metadata list is the single source of truth for the reader, the figure grid
    and the analysis prompt, so all three lose the image at once.  Blobs are only
    unlinked when no other item references the same content hash.
    """
    from ..utils import json_loads, stable_json
    row = db.one("SELECT metadata_json,html_path FROM items WHERE id=?", (item_id,))
    if not row:
        return 0
    wanted = {int(index) for index in indexes}
    metadata = json_loads(row["metadata_json"], {}) or {}
    removed = [image for image in (metadata.get("images") or [])
               if image.get("index") in wanted and image.get("local_path")]
    if not removed:
        return 0
    metadata["images"] = [image for image in (metadata.get("images") or []) if image not in removed]
    metadata["image_triage"] = {"dropped": len(removed), "note": note[:300], "at": now_iso()}
    db.execute("UPDATE items SET metadata_json=? WHERE id=?", (stable_json(metadata), item_id))
    relatives = {image.get("relative_path") for image in removed if image.get("relative_path")}
    html_path = row.get("html_path")
    if relatives and html_path and Path(html_path).is_file():
        content = Path(html_path).read_text(encoding="utf-8")
        for relative in relatives:
            content = re.sub(r'<img[^>]*src="' + re.escape(relative) + r'"[^>]*>', "", content)
        atomic_write(Path(html_path), content)
    for image in removed:
        path = Path(image.get("local_path") or "")
        if not path.is_file():
            continue
        digest = path.stem
        shared = db.one("SELECT COUNT(*) c FROM items WHERE id<>? AND metadata_json LIKE ?",
                        (item_id, f"%{digest}%"))
        if shared and int(shared["c"]) == 0:
            try:
                purge.unlink(path)
            except OSError:
                pass
    return len(removed)
