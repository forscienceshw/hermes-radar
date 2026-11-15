import re
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any

from ..db import Database
from ..net import post_form, post_json
from .digest import next_window, render_digest, sent_today, window_allows
from ..settings import get_secret, get_settings
from ..utils import head_tail, json_loads, now_iso, parse_time, redact_secrets, utcnow


def clip_bytes(text: str, limit: int) -> str:
    """Enterprise WeChat counts UTF-8 bytes; Chinese text is three bytes per character."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore").rstrip() + "…"


class QuietHours(Exception):
    """A deliberate wait, not a failure: the batch is fine, the clock is wrong."""

    def __init__(self, delay_seconds: int, reason: str = ""):
        self.delay_seconds = max(1, int(delay_seconds))
        super().__init__(reason or f"等待发送窗口，约 {max(1, self.delay_seconds // 60)} 分钟后重试")


def quiet_delay(settings: dict[str, Any], priority: str, now: datetime | None = None) -> int:
    config = settings["notifications"]
    if priority == "critical" and config.get("quiet_allow_critical", True):
        return 0
    now = now or datetime.now().astimezone()
    start_h, start_m = map(int, config["quiet_start"].split(":"))
    end_h, end_m = map(int, config["quiet_end"].split(":"))
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start == end:
        return 0
    if start < end:
        in_quiet = start <= now < end
        target = end
    else:
        in_quiet = now >= start or now < end
        target = end if now < end else end + timedelta(days=1)
    return max(1, int((target - now).total_seconds())) if in_quiet else 0


def _label(target: datetime) -> str:
    return target.astimezone().strftime("%H:%M")


def merge_closed_batches(db: Database, keep_id: str | None = None,
                         conn: Any = None) -> str | None:
    """Fold every closed-but-unsent normal batch into one digest.

    One closed batch per eight items used to sit in its own deliver job.  When the
    daily cap or a send window blocked delivery, those jobs parked themselves one by
    one and the queue showed twenty "waiting to retry" rows, each carrying a digest
    that was a day older than the next.  Merging keeps a single pending message and
    a single queue row.
    """
    context = nullcontext(conn) if conn is not None else db.transaction()
    with context as connection:
        rows = connection.execute("""SELECT id,created_at FROM notification_batches
            WHERE state='CLOSED' AND priority='normal' ORDER BY created_at""").fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return None
        keep = keep_id if keep_id in ids else ids[0]
        # A batch that is being delivered right now must not vanish under the sender.
        leased = {row["id"] for row in connection.execute("""SELECT json_extract(payload_json,'$.batch_id') AS id
            FROM jobs WHERE kind='deliver_batch' AND state='LEASED'""").fetchall() if row["id"]}
        others = [batch_id for batch_id in ids if batch_id != keep and batch_id not in leased]
        # The surviving job starts from a clean attempt counter; the hundreds it may
        # have accumulated were window waits, not failures.
        connection.execute("""UPDATE jobs SET attempts=0 WHERE kind='deliver_batch'
            AND state IN ('READY','RETRY_WAIT') AND json_extract(payload_json,'$.batch_id')=?""", (keep,))
        if others:
            connection.execute("""UPDATE jobs SET last_error=?
                WHERE kind='deliver_batch' AND state IN ('READY','RETRY_WAIT') AND last_error IS NULL
                AND json_extract(payload_json,'$.batch_id')=?""",
                               (f"等待发送窗口；已合并 {len(others)} 个积压批次", keep))
        if not others:
            return keep
        for start in range(0, len(others), 200):
            chunk = others[start:start + 200]
            marks = ",".join("?" for _ in chunk)
            connection.execute(f"UPDATE notification_items SET batch_id=? WHERE batch_id IN ({marks})",
                               [keep, *chunk])
            connection.execute(f"""UPDATE notification_batches SET state='MERGED',last_error=?
                WHERE id IN ({marks})""", [f"已并入批次 {keep[:8]}", *chunk])
            # The absorbing batch carries the delivery now; stale jobs would only
            # wake up and find a MERGED record.
            connection.execute(f"""UPDATE jobs SET state='CANCELLED',owner=NULL,lease_until=NULL,updated_at=?
                WHERE kind IN ('deliver_batch','flush_batch') AND state IN ('READY','RETRY_WAIT')
                AND json_extract(payload_json,'$.batch_id') IN ({marks})""",
                               [now_iso(), *chunk])
        return keep


def queue_for_notification(db: Database, item_id: str) -> None:
    settings = get_settings()
    config = settings["notifications"]
    item = db.one("SELECT id,importance,relevance FROM items WHERE id=?", (item_id,))
    if not item or not config["enabled"] or item["relevance"] < config["min_relevance"]:
        return
    if db.one("SELECT item_id FROM notification_items WHERE item_id=?", (item_id,)):
        return
    timestamp = now_iso()
    critical = float(item["importance"]) >= float(config["instant_threshold"])
    if critical and sent_today(db) >= int(config.get("max_per_day", 3)):
        # The cap wins: a fourth message today would only train the reader to ignore the channel.
        critical = False
    with db.transaction() as conn:
        if critical:
            batch_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO notification_batches(id,state,priority,created_at,deadline,delivery_id,channel)
                VALUES(?,'CLOSED','critical',?,?,?,?)""", (
                batch_id, timestamp, timestamp, f"delivery-{batch_id}", config["channel"],
            ))
            conn.execute("INSERT INTO notification_items(item_id,batch_id,score,reason,created_at) VALUES(?,?,?,?,?)",
                         (item_id, batch_id, item["importance"], "达到即时汇报阈值", timestamp))
            db.enqueue_job("deliver_batch", {"version": 1, "batch_id": batch_id},
                           dedupe_key=f"deliver:{batch_id}", priority=20, max_attempts=8, conn=conn)
            return
        batch = conn.execute("SELECT * FROM notification_batches WHERE state='OPEN' ORDER BY created_at LIMIT 1").fetchone()
        if batch:
            batch_id = batch["id"]
        else:
            batch_id = str(uuid.uuid4())
            # Align the batch with the next send window instead of dumping it the moment
            # a threshold is crossed; the digest is meant to arrive at a predictable hour.
            _, window_at = next_window(settings)
            latest = datetime.now().astimezone() + timedelta(minutes=float(config["max_wait_minutes"]))
            deadline = min(latest, window_at).astimezone(
                __import__("datetime").timezone.utc).isoformat(timespec="milliseconds")
            conn.execute("""INSERT INTO notification_batches(id,state,priority,created_at,deadline,delivery_id,channel)
                VALUES(?,'OPEN','normal',?,?,?,?)""", (
                batch_id, timestamp, deadline, f"delivery-{batch_id}", config["channel"],
            ))
            db.enqueue_job("flush_batch", {"version": 1, "batch_id": batch_id}, run_at=deadline,
                           dedupe_key=f"flush:{batch_id}", priority=30, max_attempts=6, conn=conn)
        conn.execute("INSERT INTO notification_items(item_id,batch_id,score,reason,created_at) VALUES(?,?,?,?,?)",
                     (item_id, batch_id, item["importance"], "普通内容积攒汇报", timestamp))
        count = conn.execute("SELECT COUNT(*) c FROM notification_items WHERE batch_id=?", (batch_id,)).fetchone()["c"]
        if count >= int(config["batch_size"]):
            conn.execute("UPDATE notification_batches SET state='CLOSED' WHERE id=? AND state='OPEN'", (batch_id,))
            keep = merge_closed_batches(db, keep_id=batch_id, conn=conn) or batch_id
            db.enqueue_job("deliver_batch", {"version": 1, "batch_id": keep},
                           dedupe_key=f"deliver:{keep}", priority=25, max_attempts=8, conn=conn)
        db.add_event("notification_queued", f"进入汇报批次 {batch_id}", item_id, conn=conn)


def flush_open_batches(db: Database, force_all: bool = False) -> int:
    timestamp = now_iso()
    batches = db.query("SELECT id,state,deadline FROM notification_batches WHERE state='OPEN'")
    count = 0
    for batch in batches:
        if force_all or batch["deadline"] <= timestamp:
            with db.transaction() as conn:
                # A manual "send now" must not be parked until the next window.
                if force_all:
                    changed = conn.execute("UPDATE notification_batches SET state='CLOSED',priority='critical' WHERE id=? AND state='OPEN'", (batch["id"],))
                else:
                    changed = conn.execute("UPDATE notification_batches SET state='CLOSED' WHERE id=? AND state='OPEN'", (batch["id"],))
                if changed.rowcount:
                    keep = merge_closed_batches(db, keep_id=batch["id"], conn=conn) or batch["id"]
                    db.enqueue_job("deliver_batch", {"version": 1, "batch_id": keep},
                                   dedupe_key=f"deliver:{keep}", priority=25, max_attempts=8, conn=conn)
                    count += 1
    return count


def render_batch(db: Database, batch_id: str) -> tuple[str, list[dict[str, Any]], str]:
    batch = db.one("SELECT * FROM notification_batches WHERE id=?", (batch_id,))
    if not batch:
        raise ValueError("汇报批次不存在")
    items = db.query("""SELECT i.*,n.score,n.reason FROM notification_items n JOIN items i ON i.id=n.item_id
        WHERE n.batch_id=? ORDER BY n.score DESC""", (batch_id,))
    title = "科研雷达：重要发现" if batch["priority"] == "critical" else f"科研雷达：{len(items)} 条新发现"
    lines = [f"# {title}", ""]
    for index, item in enumerate(items, 1):
        analysis = json_loads(item.get("analysis_json"), {}) or {}
        lines.extend([f"## {index}. {item['title'] or item['url']}",
                      f"重要度 {item['importance']:.0f}/100 · 相关度 {item['relevance']:.0f}/100 · {item['category']}", ""])
        lines.append(analysis.get("summary") or item.get("summary") or "已完成归档，暂未生成摘要。")
        questions = analysis.get("critical_questions") or analysis.get("questions") or []
        if questions:
            lines.append("\n值得追问：" + "；".join(str(q) for q in questions[:2]))
        connections = analysis.get("idea_connections") or []
        if connections:
            lines.append("\nidea 连接：" + "；".join(str(x.get("connection", "")) for x in connections[:2] if isinstance(x, dict)))
        papers = db.query("""SELECT p.doi,p.status,p.title FROM papers p JOIN item_papers ip ON ip.paper_id=p.id
            WHERE ip.item_id=?""", (item["id"],))
        if papers:
            lines.append("\n论文：" + "；".join(f"{p['doi']}（{p['status']}）" for p in papers[:5]))
        lines.extend(["", f"[阅读原文]({item['url']})", ""])
    return head_tail("\n".join(lines), 15000), items, batch["channel"]


def render_compact_wecom(db: Database, batch_id: str) -> str:
    batch = db.one("SELECT * FROM notification_batches WHERE id=?", (batch_id,))
    items = db.query("""SELECT i.*,n.score FROM notification_items n JOIN items i ON i.id=n.item_id
        WHERE n.batch_id=? ORDER BY n.score DESC LIMIT 6""", (batch_id,))
    title = "科研雷达｜优先看这条" if batch and batch["priority"] == "critical" else f"科研雷达｜{len(items)} 条速览"
    lines = [f"**{title}**", ""]
    for index, item in enumerate(items, 1):
        analysis = json_loads(item.get("analysis_json"), {}) or {}
        brief = str(analysis.get("one_sentence") or analysis.get("summary") or item.get("summary") or "已收录，等待精读。")
        brief = re.sub(r"\s+", " ", brief)
        if len(brief) > 120:
            brief = brief[:117] + "…"
        lines.append(f"**{index}. {item['title'] or item['url']}**")
        lines.append(brief)
        findings = analysis.get("key_findings") or []
        outline = []
        for finding in findings[:2]:
            text = finding.get("finding") if isinstance(finding, dict) else finding
            if text:
                outline.append(re.sub(r"\s+", " ", str(text))[:55])
        if outline:
            lines.append("提纲：" + "；".join(outline))
        lines.append(f"[原文]({item['url']})")
        lines.append("")
    lines.append("完整精读、论文与转录已保存在本机科研雷达，打开「资料库」查看。")
    return head_tail("\n".join(lines), 3500)


async def deliver_batch(db: Database, batch_id: str) -> None:
    batch = db.one("SELECT * FROM notification_batches WHERE id=?", (batch_id,))
    if not batch or batch["state"] in {"SENT", "MERGED"}:
        return
    if batch["state"] != "CLOSED":
        raise ValueError("批次尚未封闭")
    settings = get_settings()
    if batch["priority"] != "critical":
        # Fold older pending digests into this one so a backlog never becomes a
        # string of stale messages, and the queue keeps exactly one row.
        keep = merge_closed_batches(db, keep_id=batch_id)
        if keep != batch_id:
            return
        batch = db.one("SELECT * FROM notification_batches WHERE id=?", (batch_id,))
        if not batch or batch["state"] != "CLOSED":
            return
    delay = quiet_delay(settings, batch["priority"])
    if delay:
        target = datetime.now().astimezone() + timedelta(seconds=delay)
        raise QuietHours(delay, f"免打扰时段，{_label(target)} 后再发送")
    config = settings["notifications"]
    daily_cap = int(config.get("max_per_day", 3))
    if batch["priority"] != "critical":
        if sent_today(db) >= daily_cap:
            wait, target = next_window(settings)
            raise QuietHours(wait, f"今天已发满 {daily_cap} 条，{_label(target)} 再发送")
        if not window_allows(settings):
            wait, target = next_window(settings)
            raise QuietHours(wait, f"不在发送窗口，{_label(target)} 再发送")
    min_interval = int(settings["notifications"].get("min_interval_seconds", 0))
    last = db.one("SELECT sent_at FROM notification_batches WHERE state='SENT' AND sent_at IS NOT NULL ORDER BY sent_at DESC")
    if batch["priority"] != "critical" and last:
        elapsed = (utcnow() - parse_time(last["sent_at"])).total_seconds()
        # One digest per window: several batches can mature at the same hour, and sending
        # them back to back defeats the point of a scheduled summary.
        same_window = 3 * 3600
        if 0 <= elapsed < same_window:
            wait, target = next_window(settings)
            raise QuietHours(wait, f"本时段已发过一份，{_label(target)} 再发送")
        if min_interval and 0 <= elapsed < min_interval:
            remain = max(1, int(min_interval - elapsed))
            raise QuietHours(remain, f"距上次发送不足 {max(1, min_interval // 60)} 分钟，{remain // 60 + 1} 分钟后重试")
    text, items, channel = render_digest(db, batch_id, settings)
    if channel != "inbox":
        webhook = get_secret("notification_webhook")
        if not webhook:
            raise RuntimeError("尚未配置通知 Webhook；可在设置中改用站内收件箱")
        if channel == "serverchan":
            title = f"科研雷达｜{len(items)} 条速览"
            try:
                response, _ = await post_form(webhook, {"title": title, "desp": text,
                                                        "short": head_tail(items[0]["summary"] or items[0]["title"], 500)}, timeout=30)
            except Exception as exc:
                raise RuntimeError(redact_secrets(f"Server酱发送失败：{exc}")) from None
            if isinstance(response, dict) and response.get("code") not in (None, 0, "0"):
                raise RuntimeError(f"Server酱返回错误：{response.get('message') or response}")
        else:
            if channel == "wecom_webhook":
                # Enterprise WeChat allows 4096 bytes per message; Chinese costs 3 bytes a
                # character, so keep the head and tail rather than slicing mid-sentence.
                payload = {"msgtype": "markdown", "markdown": {"content": clip_bytes(head_tail(text, 1300), 3900)}}
            else:
                payload = {"delivery_id": batch["delivery_id"], "title": "Hermes 科研雷达", "text": text,
                           "items": [{"id": x["id"], "url": x["url"], "title": x["title"]} for x in items]}
            try:
                response, _ = await post_json(webhook, payload, timeout=30)
            except Exception as exc:
                raise RuntimeError(redact_secrets(f"通知发送失败：{exc}")) from None
            if channel == "wecom_webhook" and isinstance(response, dict) and response.get("errcode") not in (None, 0):
                raise RuntimeError(f"企业微信返回错误 {response.get('errcode')}：{response.get('errmsg')}")
    db.execute("""UPDATE notification_batches SET state='SENT',sent_at=?,last_error=NULL
        WHERE id=? AND state='CLOSED'""", (now_iso(), batch_id))
    db.add_event("notification_sent", f"已发送 {len(items)} 条汇报，渠道 {channel}")


async def send_test_notification() -> None:
    settings = get_settings()
    channel = settings["notifications"]["channel"]
    if channel == "inbox":
        return
    webhook = get_secret("notification_webhook")
    if not webhook:
        raise RuntimeError("未配置 Webhook")
    text = f"Hermes 科研雷达通知测试成功：{now_iso()}"
    if channel == "serverchan":
        try:
            response, _ = await post_form(webhook, {"title": "科研雷达通知测试", "desp": text, "short": "通知配置成功"}, timeout=30)
        except Exception as exc:
            raise RuntimeError(redact_secrets(f"Server酱发送失败：{exc}")) from None
        if isinstance(response, dict) and response.get("code") not in (None, 0, "0"):
            raise RuntimeError(f"Server酱返回错误：{response.get('message') or response}")
    else:
        payload = {"msgtype": "markdown", "markdown": {"content": text}} if channel == "wecom_webhook" else {
            "title": "Hermes 科研雷达测试", "text": text, "delivery_id": f"test-{uuid.uuid4()}"}
        try:
            response, _ = await post_json(webhook, payload, timeout=30)
        except Exception as exc:
            raise RuntimeError(redact_secrets(f"通知发送失败：{exc}")) from None
        if channel == "wecom_webhook" and isinstance(response, dict) and response.get("errcode") not in (None, 0):
            raise RuntimeError(f"企业微信返回错误 {response.get('errcode')}：{response.get('errmsg')}")
