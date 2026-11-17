"""坑 1 的决定性对照实验：完整链接 vs 裁掉参数的链接。

放在项目根目录运行：

    python tools/repro_url_param.py

它会从本机微信消息里取几条真实的文章卡片链接，然后对同一条链接做两次请求：

    ① 原始完整链接（含 chksm / mpshare / scene / srcid）
    ② 裁成 __biz / mid / idx / sn

判读方法：

    ① 返回几 MB 且含 js_content      → 正文，说明参数必须保留
    ② 返回约 17 KB 且含 TCaptcha     → 腾讯云滑块验证页，说明参数被裁后被判成爬虫

如果 ① 也是验证页，那才是真的被风控（换网络出口 / 配代理），见 04-验证方法.md 第 4 层。
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.wechat4_live import (DEFAULT_CACHE, canonical_url,  # noqa: E402
                                       iter_recent, message_text)

RAW_URL = re.compile(r"<url>(?:<!\[CDATA\[)?(https?://mp\.weixin\.qq\.com/s[^<\]]+?)(?:\]\]>)?</url>")

# 和项目里归档器用同一套请求头，避免"头不同"造成误判
WECHAT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://mp.weixin.qq.com/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def classify(html: str) -> str:
    if "js_content" in html or "rich_media_content" in html:
        return "正文 ✅"
    if "TCaptcha" in html or "cap_sid" in html or "cgiData" in html:
        return "滑块验证码 ❌"
    if "环境异常" in html:
        return "环境异常 ❌"
    if "参数错误" in html:
        return "参数错误 ❌"
    return "其他 ❓"


def fetch(url: str) -> tuple[int, int, str]:
    request = urllib.request.Request(url, headers=WECHAT_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            html = response.read().decode("utf-8", "replace")
    except Exception as exc:                          # noqa: BLE001
        return 0, 0, f"FAIL {type(exc).__name__}: {exc}"
    return response.status, len(html), classify(html)


def samples(limit: int = 3) -> list[str]:
    """取几条**文章型**链接（跳过 t=pages/ 图片分享页，那种本来就拿不到正文）。"""
    found: list[str] = []
    for db_rel in ("message/message_0.db", "message/biz_message_0.db"):
        for _username, row in iter_recent(DEFAULT_CACHE, db_rel, limit=400):
            text = message_text(row)
            match = RAW_URL.search(text or "")
            if not match:
                continue
            url = match.group(1)
            if "t=pages/" in url:          # 图片/视频分享页，不是文章，跳过
                continue
            found.append(url)
            if len(found) >= limit:
                return found
    return found


def main() -> int:
    rows = samples()
    if not rows:
        print("没从本地消息里取到公众号文章链接。")
        print("先在微信里给自己转发一篇公众号文章，再跑一次。")
        return 2
    verdict = None
    for raw in rows:
        full = raw.replace("&amp;", "&").replace("&quot;", '"').strip()
        short = canonical_url(full)
        print("=" * 78)
        print("原始链接：", full[:160])
        status, size, state = fetch(full)
        print(f"  ① 完整原始链接      HTTP {status} len={size:8}  {state}")
        if full == short:
            print("  ② 与原始一致（这条本来就短），换下一条")
            continue
        status2, size2, state2 = fetch(short)
        print(f"  ② 裁到 biz/mid/idx/sn HTTP {status2} len={size2:8}  {state2}")
        if "✅" in state:
            verdict = "ok"
        elif "❌" in state:
            verdict = "blocked"

    print()
    print("=" * 78)
    if verdict == "ok":
        print("结论：完整链接能拿到正文。集成时务必保留原始 query 参数（坑 1）。")
        return 0
    if verdict == "blocked":
        print("结论：连完整链接都被挡。这才是真的风控/网络问题，见 04-验证方法.md 第 4 层。")
        return 1
    print("结论：样本不够，无法判断。多取几条，或在微信里转发一篇新文章再试。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
