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


def _wechat_state() -> dict[str, Any]:
    """体检里的"微信数据读取"。

    只看进程名会骗人：微信 4.x 起来了、`Weixin.exe` 也在，3.x 的探针照样会报
    "就绪"，但真正能读到的库根本不是那一个。所以这里以 4.x 通道为准，并且**真的
    数一遍近 7 天读到多少篇文章**，读不到就如实写 0，而不是给个绿点。
    """
    try:
        from . import wechat4_live
        status = wechat4_live.quick_status()
    except Exception:                                   # noqa: BLE001
        status = {"running": False, "storages": [], "databases": 0}
    if status.get("storages"):
        try:
            diagnose = wechat4_live.diagnose(days=7)
        except Exception as exc:                        # noqa: BLE001
            diagnose = {"articles": 0, "newest": "", "shards": [],
                        "error": f"{type(exc).__name__}: {exc}"[:200]}
        # 读到多少是一回事，其中多少来自你勾选的号是另一回事。只报前者会让人
        # 以为"读到了但没入库"，只报后者在没勾号时又永远是 0。
        try:
            import time as _time
            from .wechat_self_archive import _public_account_filter
            directory = wechat4_live.oa_directory()
            allowed, _, _ = _public_account_filter({"use_catalog": True}, directory)
            recent = wechat4_live.official_articles(since_ts=int(_time.time()) - 7 * 86400)
            diagnose["total"] = len(recent)
            diagnose["tracked_hits"] = len([item for item in recent if item["user_name"] in allowed])
        except Exception:                               # noqa: BLE001
            pass
        detail = f"微信 4.x · 近 7 天读到 {diagnose.get('articles', 0)} 篇"
        if "tracked_hits" in diagnose:
            detail += f"，命中追踪名单 {diagnose['tracked_hits']} 篇"
        elif diagnose.get("newest"):
            detail += f"，最新 {diagnose['newest'][5:]}"
        # 微信收到多少 vs 我们能读到多少。两个数差得远，说明是客户端没存正文
        # （升级重装后 4.x 只导入了最近几小时），不是雷达没读到——必须写清楚，
        # 否则用户只看到一个绿点却在资料库里找不到东西。
        pushes = diagnose.get("pushes") or {}
        if pushes.get("accounts"):
            detail += (f"；同期微信里 {pushes['accounts']} 个号推过，"
                       f"本地只留下了 {diagnose.get('articles', 0)} 篇正文")
            if diagnose.get("articles", 0) < pushes["accounts"] // 4:
                diagnose["thin"] = True
        # 公众号库写不动了——这才是"读不到新推文"的真凶。库里的条数和最后写入
        # 时间都摆出来，用户一眼能看出是微信自己没存，而不是雷达没读。
        biz = diagnose.get("biz_write") or {}
        sess = diagnose.get("session") or {}
        # 凌晨 2~6 点公众号本来就少，其余时段两小时没动静就算异常
        quiet_hours = 10 if _time.localtime().tm_hour in (2, 3, 4, 5, 6) else 2
        if biz.get("ago") is not None:
            detail += (f"；公众号库共 {diagnose.get('biz_rows', 0)} 条，"
                       f"最后写入 {biz['at'][5:]}")
            # 深夜公众号本来就少，白天两小时没动静才算真停了
            if float(biz["ago"]) >= quiet_hours:
                diagnose["stalled"] = round(float(biz["ago"]))
        # 会话表比库文件更能说明问题：私聊一直在动、公众号会话一条不动，就是
        # 客户端根本没在收公众号推送（4.x 升级后把通知开关回退了），不是读错库。
        if sess.get("last_ago") is not None:
            detail += (f"；公众号会话最后动静 {sess['last_display']}，"
                       f"近 3 小时 {sess['recent']} 个号推过")
            if not sess["recent"] and float(sess["last_ago"]) >= quiet_hours:
                diagnose["silent"] = round(float(sess["last_ago"]))
        # 升级前的推文全在 3.x 的那个库里，4.x 客户端永远补不出来。单独报一句，
        # 免得用户以为"一升级东西就没了"。
        try:
            from . import wechat3_public
            history = wechat3_public.stats()
        except Exception:                               # noqa: BLE001
            history = {"ok": False}
        if history.get("ok"):
            import time as _h3
            detail += (f"；升级前的 3.x 库还有 {history['total']} 条，最新到 "
                       f"{_h3.strftime('%m-%d %H:%M', _h3.localtime(history['newest']))}")
            diagnose["history"] = history
        if diagnose.get("error"):
            detail += f"（{diagnose['error']}）"
        return {"state": "ready", "version": "4.x", "channel": "4.x",
                "detail": detail, "diagnose": diagnose,
                "databases": int(status.get("databases") or 0)}
    legacy = wechat_probe.status()
    return {**legacy, "channel": legacy.get("family") or "3.x"}


def _space_state() -> dict[str, Any]:
    try:
        from . import wechat_space
        return wechat_space.snapshot()
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "warnings": [f"{type(exc).__name__}: {exc}"], "sizes": {}}


def _backup_state() -> dict[str, Any]:
    try:
        from . import backup
        return backup.status()
    except Exception as exc:                            # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def snapshot() -> dict[str, Any]:
    settings = get_settings()
    secrets = secret_status()
    llm = _llm_state(settings, secrets)
    notify = _notify_state(settings, secrets)
    wechat = _wechat_state()
    space = _space_state()
    auto = autostart.status()
    db = _db_info()
    accounts = _account_info()

    wechat_ok = wechat["state"] in {"ready", "offline"}
    # 通道通、但微信本地只存了零星正文 → 这一项给黄灯而不是绿灯。绿灯会让人以为
    # "都好了"，可资料库里就是没东西；黄灯配一句能照做的提示才有用。
    diagnose = wechat.get("diagnose") or {}
    thin = bool(diagnose.get("thin"))
    stalled = diagnose.get("stalled")
    silent = diagnose.get("silent")
    session = diagnose.get("session") or {}
    if wechat["state"] == "unsupported-4x":
        wechat_detail = "微信 4.x 尚未适配，暂时读不到它的数据库"
    elif wechat["state"] == "ready":
        wechat_detail = wechat.get("detail") or f"微信 {wechat['version']} 已就绪"
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
        {"id": "wechat", "label": "微信数据读取",
         "state": "warn" if (thin or stalled or silent or not wechat_ok) else "ok",
         "detail": (
             f"公众号会话最后一次动静是 {session.get('last_display', '—')}，之后 {silent} 小时"
             f"一条都没进来；同一时间私聊还在收（{session.get('chat_display', '—')}）。"
             "微信 4.x 不会主动把公众号推文存到本地，得让它去服务端拉一次。三个入口任选一个："
             "① 电脑微信「通讯录 → 公众号」，随便点开一个号再返回"
             "（这一步最稳，1714 个号都在这儿）；"
             "② 聊天列表顶部搜索框输入「订阅号」，点那条「订阅号消息」再往下拉；"
             "③ 左侧竖排图标里找书本/报纸形状的「订阅号」。"
             "拉过一次之后雷达每 30 秒就能自动跟上，不用再点。"
             if silent else
             f"{wechat_detail}。微信已经 {stalled} 小时没往公众号库里写过东西了——"
             "客户端自己没在收，不是雷达读不到。照上面的入口点开一次「订阅号消息」"
             "并往下拉，再点这里的「重新检查」"
             if stalled else
             (wechat_detail + "。微信 4.x 只在打开订阅号时去服务端拉，"
                              "点开一次「订阅号消息」并往下拉，这里就会涨上去") if thin else wechat_detail)},
        {"id": "llm", "label": "大模型精读", "state": "ok" if llm["configured"] else "warn",
         "detail": "已配置" if llm["configured"] else ("未填 Key" if llm["needs_key"] else "未启用")},
        {"id": "notify", "label": "汇报渠道", "state": "ok" if notify["configured"] else "warn",
         "detail": "站内收件箱" if notify["channel"] == "inbox" else
                   ("已保存发送地址" if notify["webhook_saved"] else "缺少发送地址")},
        {"id": "accounts", "label": "公众号名单", "state": "ok" if accounts["enabled"] else "warn",
         "detail": f"{accounts['enabled']} 个号在追更" if accounts["enabled"] else "还没有启用任何公众号"},
        {"id": "space", "label": "微信文件没有堆在 C 盘",
         "state": "ok" if not (space.get("warnings") or []) else "warn",
         "detail": ("；".join(space.get("warnings") or [])
                    or f"C 盘剩余 {space['free_c'] / 1024 ** 3:.0f} GB，微信数据在外置目录")},
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
        "space": space,
        "backup": _backup_state(),
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
