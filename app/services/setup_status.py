"""One read-only snapshot that drives the whole setup wizard.

The wizard is a single scrolling page: every step shows its own state, and the
top of the page shows how many steps are already done.  Everything it needs is
computed here, so the browser does not have to stitch five endpoints together
and the answer stays consistent between steps.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from ..paths import BASE_DIR, DATA_DIR
from ..settings import get_settings, secret_status
from . import autostart, wechat_probe


def project_version() -> str:
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return "0.0.0"


def _db_info() -> dict[str, Any]:
    from ..db import Database
    from ..paths import DB_PATH
    try:
        db = Database(DB_PATH)
        row = db.one("""SELECT COUNT(*) total,
            SUM(excluded_at IS NULL) visible,
            SUM(excluded_at IS NULL AND status='ready') ready,
            SUM(excluded_at IS NULL AND status='error') errors
            FROM items""") or {}
        heartbeat = db.one("SELECT value FROM runtime WHERE key='worker'")
    except Exception as exc:  # a broken database must not break the wizard
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    from ..utils import parse_time, utcnow
    beat = (heartbeat or {}).get("value")
    alive = bool(beat and parse_time(beat) and (utcnow() - parse_time(beat)).total_seconds() < 15)
    return {
        "ok": True,
        "items": int(row.get("total") or 0),
        "visible": int(row.get("visible") or 0),
        "ready": int(row.get("ready") or 0),
        "errors": int(row.get("errors") or 0),
        "worker_alive": alive,
        "worker_heartbeat": beat or "",
    }


def _account_info() -> dict[str, Any]:
    from ..db import Database
    from ..paths import DB_PATH
    try:
        row = Database(DB_PATH).one(
            "SELECT COUNT(*) total, SUM(enabled) enabled FROM wechat_accounts") or {}
    except Exception:
        return {"total": 0, "enabled": 0}
    return {"total": int(row.get("total") or 0), "enabled": int(row.get("enabled") or 0)}


def _llm_state(settings: dict[str, Any], secrets: dict[str, Any]) -> dict[str, Any]:
    llm = settings.get("llm") or {}
    provider = llm.get("provider") or "openai_compatible"
    needs_key = provider == "openai_compatible"
    configured = bool(llm.get("enabled")) and (bool(secrets.get("llm_api_key")) or not needs_key)
    return {
        "enabled": bool(llm.get("enabled")),
        "provider": provider,
        "model": llm.get("model") or "",
        "base_url": llm.get("base_url") or "",
        "vision": bool(llm.get("vision_enabled")),
        "needs_key": needs_key,
        "key_saved": bool(secrets.get("llm_api_key")),
        "configured": configured,
        "workbuddy_ok": provider == "workbuddy_cloud",
    }


def _notify_state(settings: dict[str, Any], secrets: dict[str, Any]) -> dict[str, Any]:
    notifications = settings.get("notifications") or {}
    channel = notifications.get("channel") or "inbox"
    needs_webhook = channel in ("wecom_webhook", "serverchan", "generic_webhook")
    saved = bool(secrets.get("notification_webhook"))
    return {
        "channel": channel,
        "needs_webhook": needs_webhook,
        "webhook_saved": saved,
        "configured": saved if needs_webhook else True,
    }


def snapshot() -> dict[str, Any]:
    settings = get_settings()
    secrets = secret_status()
    llm = _llm_state(settings, secrets)
    notify = _notify_state(settings, secrets)
    wechat = wechat_probe.status()
    auto = autostart.status()
    db = _db_info()
    accounts = _account_info()

    wechat_ok = wechat["state"] in {"ready", "offline"}
    if wechat["state"] == "unsupported-4x":
        wechat_detail = "微信 4.x 尚未适配，暂时读不到它的数据库"
    elif wechat["state"] == "ready":
        wechat_detail = f"微信 {wechat['version']} 已就绪"
    elif wechat["state"] == "offline":
        wechat_detail = "上次的账号与密钥还在，可以直接读离线目录"
    elif wechat["state"] == "need-login":
        wechat_detail = "需要微信保持登录才能授权读取"
    elif wechat["state"] == "version-unknown":
        wechat_detail = f"微信 {wechat['version']} 未在验证表内，可尝试"
    else:
        wechat_detail = wechat.get("hint") or "没找到微信"

    checks = [
        {"id": "service", "label": "服务在本机运行", "state": "ok" if db.get("ok") else "error",
         "detail": f"127.0.0.1:{settings.get('port')} · 版本 {project_version()}"},
        {"id": "worker", "label": "后台处理进程", "state": "ok" if db.get("worker_alive") else "warn",
         "detail": "心跳正常" if db.get("worker_alive") else "没有心跳，抓取和精读会停住"},
        {"id": "wechat", "label": "微信数据读取", "state": "ok" if wechat_ok else "warn",
         "detail": wechat_detail},
        {"id": "llm", "label": "大模型精读", "state": "ok" if llm["configured"] else "warn",
         "detail": "已配置" if llm["configured"] else ("未填 Key" if llm["needs_key"] else "未启用")},
        {"id": "notify", "label": "汇报渠道", "state": "ok" if notify["configured"] else "warn",
         "detail": "站内收件箱" if notify["channel"] == "inbox" else
                   ("已保存发送地址" if notify["webhook_saved"] else "缺少发送地址")},
        {"id": "accounts", "label": "公众号名单", "state": "ok" if accounts["enabled"] else "warn",
         "detail": f"{accounts['enabled']} 个号在追更" if accounts["enabled"] else "还没有启用任何公众号"},
        {"id": "autostart", "label": "开机自启", "state": "ok" if auto["enabled"] else "off",
         "detail": ("旧版启动文件夹方式，建议换成注册表" if auto["mode"] == "legacy_shortcut"
                    else ("已开启" if auto["enabled"] else "关闭（可随时打开）"))},
    ]

    steps = [
        {"id": "wechat", "label": "连接微信", "done": wechat_ok},
        {"id": "llm", "label": "接入大模型", "done": llm["configured"]},
        {"id": "notify", "label": "选择汇报方式", "done": notify["configured"]},
        {"id": "accounts", "label": "挑选公众号", "done": bool(accounts["enabled"])},
        {"id": "autostart", "label": "开机自启", "done": bool(auto["enabled"])},
    ]

    return {
        "version": project_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "install_dir": str(BASE_DIR),
        "data_dir": str(DATA_DIR),
        "python": sys.version.split()[0],
        "port": settings.get("port"),
        "host": settings.get("host"),
        "db": db,
        "wechat": wechat,
        "llm": llm,
        "notify": notify,
        "accounts": accounts,
        "autostart": auto,
        "checks": checks,
        "steps": steps,
        "progress": {
            "done": sum(1 for step in steps if step["done"]),
            "total": len(steps),
        },
        "env_warnings": _env_warnings(),
    }


def _env_warnings() -> list[str]:
    warnings: list[str] = []
    if not (BASE_DIR / "cloud" / "dist" / "llm-worker.mjs").is_file():
        warnings.append("缺少 cloud/dist/llm-worker.mjs，WorkBuddy 点数模式不可用；可改用 API Key 或 Ollama。")
    if not (BASE_DIR / "analysis" / "wechatmsg_full" / "app" / "decrypt" / "get_wx_info.py").is_file():
        warnings.append("缺少微信只读解密模块（analysis/wechatmsg_full），微信读取不可用。")
    if os.name == "nt" and not (BASE_DIR / "Radar.exe").is_file():
        warnings.append("当前是以源码方式运行（没有 Radar.exe），开机自启会调用 pythonw。")
    try:
        import sqlcipher3  # noqa: F401
    except ImportError:
        warnings.append("未安装 sqlcipher3，无法读取微信数据库。")
    return warnings
