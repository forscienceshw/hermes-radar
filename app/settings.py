import base64
import copy
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .paths import SECRETS_PATH, SETTINGS_PATH

DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "host": "127.0.0.1",
    "port": 8766,
    "worker_concurrency": 3,
    "default_poll_seconds": 300,
    "request_timeout_seconds": 35,
    "max_html_mb": 25,
    # 留空 = 直连。有些站点（微信文章页尤其明显）按 IP 段上风控，换出口是唯一
    # 有效的解法：填 http://host:port 或 socks5://user:pass@host:port 即可。
    "http_proxy": "",
    # 自动更新：只在启动时查一次"有没有新版本"，装不装由人决定。下载地址默认走
    # GitHub Release，国内慢的话可以在这里填一个前缀（如 https://ghproxy.net/）。
    "auto_update_check": True,
    "update_mirror": "",
    # 图片是磁盘占用的大头：40 张 × 15 MB 的上限意味着一篇长图文能吃掉几百 MB，
    # 而真正会回看的通常只有开头几张图。
    "max_images_per_article": 12,
    "max_image_mb": 6,
    "image_triage_enabled": True,
    "image_triage_max": 20,
    "science_keywords": [
        "研究", "论文", "机制", "细胞", "蛋白", "基因", "实验", "临床", "综述",
        "Nature", "Science", "Cell", "DOI", "研究进展", "研究发现", "前沿",
        "research", "study", "abstract", "methods", "results", "experiment", "clinical",
        "protein", "genome", "mechanism", "review", "trial", "preprint", "journal",
        "scientific", "analysis", "therapy", "neuron", "molecular",
    ],
    "exclude_keywords": ["广告", "推广", "课程报名", "招聘"],
    "llm": {
        "enabled": False,
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        # 走 WorkBuddy 点数时，精读自动挑当前单价最低的模型（免费优先）。
        # 只对 provider=workbuddy_cloud 生效；关掉就用上面那个固定 model。
        "auto_cheapest": True,
        "vision_enabled": False,
        "timeout_seconds": 900,
        "stall_seconds": 300,
        "max_input_chars": 80000,
        "min_local_relevance": 40,
        "long_material_strategy": "head_tail",
        "max_ideas": 8,
        "max_images": 6,
        # A local fuse, not a WorkBuddy quota: one reading or one image triage
        # costs roughly 0.15 points, so 1200 calls is a few hundred points.
        # It exists to stop a runaway loop from draining the account overnight.
        "daily_call_limit": 1200,
        "chat_context_tokens": 0,
        "accounts": [],
    },
    "papers": {
        "enabled": True,
        "email": "",
        "max_pdf_mb": 100,
        "max_per_item": 10,
        "extract_text": True,
        "download_references": False,
        # 正文里没有 DOI 时，按「正文链接反推」和「引号里的论文标题」再找一遍。
        "title_lookup": True,
    },
    "notifications": {
        "enabled": True,
        "channel": "inbox",
        "batch_size": 8,
        "max_wait_minutes": 180,
        "instant_threshold": 88,
        "min_relevance": 20,
        "min_interval_seconds": 600,
        "quiet_start": "23:30",
        "quiet_end": "07:30",
        "quiet_allow_critical": True,
        "send_windows": ["08:30", "13:00", "20:30"],
        "max_per_day": 3,
        "highlight_threshold": 74,
        "digest_limit": 10,
        "focus_topics": ["DNA 合成", "RNA 合成", "纳米结构", "适配体", "传感器", "神经科学", "结构生物学"],
    },
    "video": {
        "asr_enabled": False,
        "asr_model": "small",
        "asr_device": "auto",
        "keep_audio": False,
        "max_duration_minutes": 180,
    },
    "analysis": {
        "custom_reading_prompt": "",
        "prompt_version": "research-reader-v2",
        # 同时有几篇在精读。精读是网络等待型任务（httpx 异步），并发比串行快得多；
        # 上限受云模型端限流和本机带宽约束，8 已经够用，再高收益很小。
        "concurrency": 3,
    },
    # 点资料库条目时的排布。两套界面都保留，这里只决定用哪一套：
    # workbench = 先看内嵌离线页，精读/笔记/Ideas 可并排；classic = 经典精读弹层，
    # 离线页在浏览器新标签打开。改完即时生效，不需要重启服务。
    "ui": {
        "reading_mode": "workbench",
    },
}

_LOCK = threading.RLock()


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def get_settings() -> dict[str, Any]:
    with _LOCK:
        if not SETTINGS_PATH.exists():
            return copy.deepcopy(DEFAULTS)
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 配置文件被改坏（多半是手动编辑少了逗号）时，不能让它把整个服务拖死。
            # 留一份残骸方便找回，然后按默认值起来，用户还能进设置页改。
            broken = SETTINGS_PATH.with_name(f"settings.json.broken-{int(time.time())}")
            try:
                SETTINGS_PATH.replace(broken)
                print(f"[settings] 配置文件解析失败（{exc}），已挪到 {broken.name}，本次使用默认值",
                      file=sys.stderr, flush=True)
            except OSError:
                print(f"[settings] 配置文件解析失败：{exc}", file=sys.stderr, flush=True)
            return copy.deepcopy(DEFAULTS)
        if not isinstance(data, dict) or data.get("schema_version") != DEFAULTS["schema_version"]:
            raise ValueError("不支持的设置版本")
        return _merge(DEFAULTS, data)


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    if "schema_version" in patch and patch["schema_version"] != 1:
        raise ValueError("不支持的设置版本")
    with _LOCK:
        current = get_settings()
        updated = _merge(current, patch)
        updated["schema_version"] = 1
        _atomic_json(SETTINGS_PATH, updated)
        return updated


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("密钥保护目前仅支持 Windows DPAPI")
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _Blob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _Blob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _read_secrets() -> dict[str, str]:
    if not SECRETS_PATH.exists():
        return {}
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_secret(name: str, value: str) -> None:
    allowed = {"llm_api_key", "datax_api_key", "notification_webhook", "wecom_kf_secret", "wechat_db_key"}
    # Per-provider keys are stored under "provider:<account id>".
    if name not in allowed and not name.startswith("provider:"):
        raise ValueError("未知密钥项")
    with _LOCK:
        secrets = _read_secrets()
        if value:
            secrets[name] = base64.b64encode(_crypt(value.encode("utf-8"), True)).decode("ascii")
        else:
            secrets.pop(name, None)
        _atomic_json(SECRETS_PATH, secrets)


def get_secret(name: str) -> str:
    with _LOCK:
        encoded = _read_secrets().get(name)
        if not encoded:
            return ""
        try:
            return _crypt(base64.b64decode(encoded), False).decode("utf-8")
        except Exception:
            # DPAPI 密文只在这台机器、这个 Windows 账户下能解。整个目录拷到
            # 别的电脑后旧密文必然解不开——这时必须当成"没配"，否则程序一启动
            # 就崩，用户连界面都进不去，更别说重新填密钥。
            return ""


def secret_status() -> dict[str, Any]:
    secrets = _read_secrets()
    status: dict[str, Any] = {name: bool(secrets.get(name))
                              for name in ("llm_api_key", "datax_api_key", "notification_webhook",
                                           "wecom_kf_secret", "wechat_db_key")}
    status["providers"] = sorted(name.split(":", 1)[1] for name in secrets if name.startswith("provider:"))
    # 有值但解不开，说明是从别的机器/账户带过来的。界面该提示重新填一次。
    status["broken"] = sorted(name for name, value in secrets.items()
                              if value and not get_secret(name))
    return status
