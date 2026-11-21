import asyncio
import ctypes
import logging
import os
import random
import signal
import socket
import uuid
from datetime import datetime, timedelta, timezone

from .db import Database
from .logging_config import configure_logging
from .pipeline import JobRunner
from .services.notify import QuietHours, merge_closed_batches
from .services.archive import WechatNeedsVerification
from .services.analyzer import DailyLimitReached, requeue_limit_blocked
from .services.relevance import cleanup_existing_items, cleanup_marketing_items, dedupe_duplicate_items
from .settings import get_settings
from .utils import job_error_is_environmental, json_loads, now_iso, stable_json

log = logging.getLogger(__name__)
_MUTEX_HANDLE = None

# 关注列表的同步间隔。定 30 分钟是因为代价只有「解一次密 + 读一张联系人表」，
# 而按天同步的体感就是「我上午关注的号，第二天才出现」。
ACCOUNTS_SYNC_INTERVAL = 30 * 60


def single_instance() -> bool:
    """同一个数据目录只允许一个 Worker。

    互斥量名带上数据目录的指纹：机器上同时存在两个实例（升级期间新旧并存、
    或者开发实例和正式实例一起开着）时，各自跑各自的，不会互相把对方挤掉。
    """
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    import hashlib
    from .paths import DATA_DIR
    tag = hashlib.sha1(str(DATA_DIR).casefold().encode("utf-8")).hexdigest()[:12]
    _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(
        None, False, f"Local\\HermesResearchRadarWorker-{tag}")
    return ctypes.windll.kernel32.GetLastError() != 183


def activate_pending_sources(db: Database) -> int:
    """One-shot activation for newly validated local sources after a code reload."""
    activated = 0
    for source in db.query("SELECT * FROM sources WHERE type IN ('wechat_local','wechat_public_local')"):
        config = json_loads(source["config_json"], {}) or {}
        if not config.pop("enable_after_restart", False):
            continue
        timestamp = now_iso()
        db.execute("""UPDATE sources SET enabled=1,next_poll_at=?,config_json=?,error_count=0,
            last_error=NULL,updated_at=? WHERE id=?""", (timestamp, stable_json(config), timestamp, source["id"]))
        db.add_event("source_activated", f"新版本已加载，自动启用：{source['name']}", source_id=source["id"])
        activated += 1
    return activated


class Worker:
    def __init__(self):
        self.db = Database()
        self.runner = JobRunner(self.db)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stop_event = asyncio.Event()
        # 精读是网络等待型任务，几篇一起跑才有意义。这个上限只限制 analyze_item，
        # 轮询/归档照常走，不会被精读堵住。
        self.analysis_limit = self._read_analysis_limit()
        self._analyze_running = 0
        self.loop_tasks: list[asyncio.Task] = []
        self.tasks: list[asyncio.Task] = []

    @staticmethod
    def _read_analysis_limit() -> int:
        raw = (get_settings().get("analysis") or {}).get("concurrency", 3)
        try:
            return max(1, min(8, int(raw)))
        except (TypeError, ValueError):
            return 3

    def _desired_loops(self) -> int:
        """总协程数要盖住「精读上限 + 干杂活的槽位」，否则并到不了设定的篇数。"""
        try:
            base = max(1, min(8, int(get_settings()["worker_concurrency"])))
        except (TypeError, ValueError):
            base = 3
        return max(base, min(12, self.analysis_limit + 1))

    def _skip_kinds(self) -> tuple[str, ...]:
        """精读排满就只跳过精读，轮询/归档不受影响。"""
        return ("analyze_item",) if self._analyze_running >= self.analysis_limit else ()

    def stop(self, *args) -> None:
        self.stop_event.set()

    async def scheduler(self) -> None:
        while not self.stop_event.is_set():
            try:
                for source in self.db.source_due():
                    interval = max(30, int(source["poll_seconds"] or 300))
                    outstanding = self.db.one("""SELECT id FROM jobs WHERE kind='poll_source'
                        AND state IN ('READY','RETRY_WAIT','LEASED')
                        AND json_extract(payload_json,'$.source_id')=?""", (source["id"],))
                    now = datetime.now(timezone.utc)
                    delay = interval + random.uniform(0, min(30.0, interval * 0.15))
                    next_poll = (now + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
                    if not outstanding:
                        window = int(now.timestamp() // interval)
                        self.db.enqueue_job("poll_source", {"version": 1, "source_id": source["id"]},
                                            dedupe_key=f"poll:{source['id']}:{window}", priority=90, max_attempts=5)
                    self.db.execute("UPDATE sources SET next_poll_at=?,updated_at=? WHERE id=?", (next_poll, now_iso(), source["id"]))
            except Exception:
                log.exception("调度来源失败")
            await asyncio.sleep(0.7)

    async def heartbeat(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.db.heartbeat("worker")
                self.db.recover_expired_jobs()
            except Exception:
                log.exception("Worker 心跳失败")
            await asyncio.sleep(3)

    async def backup_loop(self) -> None:
        """每天给资料库拍一张快照，顺便让它成为"误删也能找回"的兜底。"""
        from .services import backup

        while not self.stop_event.is_set():
            try:
                result = backup.run()
                if result.get("created"):
                    log.info("资料库快照：%s（%.1f MB）", result["created"],
                             result.get("size", 0) / 1024 / 1024)
            except Exception:                       # noqa: BLE001 - 备份失败不该拖垮主流程
                log.exception("资料库快照失败")
            try:
                swept = backup.sweep_trash()
                if swept:
                    log.info("回收站里本项目的记录清掉 %d 条", swept)
            except Exception:                       # noqa: BLE001 - 清扫失败也不影响主流程
                log.exception("清扫回收站失败")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=6 * 3600)
                return
            except asyncio.TimeoutError:
                continue

    def _accounts_sync_due(self) -> bool:
        stamp = self.db.get_runtime("wechat_accounts_synced_at") or ""
        if not stamp:
            return True
        try:
            last = datetime.fromisoformat(stamp)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() >= ACCOUNTS_SYNC_INTERVAL

    async def account_sync_loop(self) -> None:
        """每半小时把本机新关注的公众号补进库：只新增、不追踪。

        「读到关注列表」和「决定收谁的推文」是两件事。库里的 1741 条是 11-13
        一次性 OCR 快照，之后再关注一个号，库里根本不会出现——用户的体感就是
        「我明明关注了，公众号库里却没有」。这里读本机 contact.db 补齐，
        新号一律 enabled=0，收不收仍由用户在面板里勾。

        间隔定 30 分钟而不是一天：解一次密读一张联系人表只要一秒，按天同步的
        代价是用户上午关注的号要到第二天才出现，看起来就是「同步坏了」。

        两个坑：
        * ``refresh()``/``account_rows()`` 都是同步的磁盘活（SQLCipher 解密 + 全表读），
          直接在事件循环里跑会把采集、监听、任务队列一起卡住几十秒，必须扔进线程。
        * 微信没登录时 4.x 根本没有密钥，读不到联系人表。这一趟不能算数——
          不写 ``wechat_accounts_synced_at``，一小时后再试，否则一天只有一次机会，
          刚好撞上关机就白等 24 小时。
        """
        from .services import wechat4_live, wechat_panel

        while not self.stop_event.is_set():
            wait = ACCOUNTS_SYNC_INTERVAL
            try:
                if self._accounts_sync_due():
                    status = await asyncio.to_thread(wechat4_live.quick_status)
                    if not status.get("running"):
                        wait = 3600
                        log.info("微信没在运行，关注列表同步推迟到一小时后重试")
                    else:
                        await asyncio.to_thread(wechat4_live.refresh)
                        rows = await asyncio.to_thread(wechat4_live.account_rows)
                        if rows:
                            result = wechat_panel.sync_account_rows(self.db, rows, "4.x")
                            self.db.set_runtime("wechat_accounts_synced_at", now_iso())
                            if result["inserted"] or result["merged"]:
                                log.info("关注列表同步：新增 %s 个、按名字归并 %s 个（默认不追踪）",
                                         result["inserted"], result["merged"])
                        else:
                            wait = 3600
                            log.info("关注列表同步读到 0 个公众号，一小时后重试")
            except Exception:                    # noqa: BLE001 - 同步失败不该拖垮采集
                log.exception("同步本机公众号关注列表失败")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                continue

    async def jsonl_watcher(self) -> None:
        """JSONL is a local tail, not a poll job: avoid thousands of queue rows per day."""
        while not self.stop_event.is_set():
            try:
                sources = self.db.query("SELECT * FROM sources WHERE type='jsonl' AND enabled=1")
                for source in sources:
                    await self.runner._poll({"id": f"jsonl-watch:{source['id']}"},
                                            {"source_id": source["id"]})
            except Exception:
                log.exception("分享桥监听失败")
            await asyncio.sleep(2)

    async def wechat_watcher(self) -> None:
        """盯 `biz_message_*.db` 的写入指纹，变了就立刻补采一轮。

        这是**补历史**用的，不是主通道：日常新推送由内存盒子通道每 30 秒扫一次，
        不点「订阅号」也收得到。微信 4.x 只在打开「订阅号」时才把攒着的消息落盘，
        所以点开那一刻指纹会变——立刻补采，不用等下一个轮询周期，也不会让用户
        觉得"点了没反应"。
        """
        from .services import wechat4_live

        last = None
        while not self.stop_event.is_set():
            try:
                marks = wechat4_live.biz_fingerprint()
                if marks and last is not None and marks != last:
                    sources = self.db.query(
                        "SELECT * FROM sources WHERE type='wechat_public_local' AND enabled=1")
                    for source in sources:
                        log.info("微信刚写入了公众号消息，立即补采 %s", source["name"])
                        await self.runner._poll({"id": f"wechat-watch:{source['id']}"},
                                                {"source_id": source["id"]})
                last = marks
            except Exception:                       # noqa: BLE001 - 监听失败不该拖垮主流程
                log.exception("公众号落盘监听失败")
            await asyncio.sleep(60)

    async def settings_watch(self) -> None:
        """设置页改了「同时精读几篇」要立刻生效，不能等重启。"""
        while not self.stop_event.is_set():
            try:
                self.analysis_limit = self._read_analysis_limit()
                self._ensure_loops(self._desired_loops())
            except Exception:
                log.exception("读取并发设置失败")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=5)
                return
            except asyncio.TimeoutError:
                continue

    async def budget_watch(self) -> None:
        """额度被调高时，把顺延到明天的那批任务提前放回来。"""
        while not self.stop_event.is_set():
            try:
                from .services.analyzer import resume_budget_parked
                freed = resume_budget_parked(self.db)
                if freed:
                    log.info("额度还有余量，把 %s 个顺延的精读任务提前放回来", freed)
            except Exception:                       # noqa: BLE001 - 看门狗不该拖垮主流程
                log.exception("恢复顺延任务失败")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=30)
                return
            except asyncio.TimeoutError:
                continue

    def _ensure_loops(self, wanted: int) -> None:
        """按需补协程；只增不减，避免半路取消正在跑的任务。"""
        while len(self.loop_tasks) < wanted:
            self.loop_tasks.append(asyncio.create_task(self.loop(len(self.loop_tasks))))
            self.tasks.append(self.loop_tasks[-1])
            log.info("并发槽位增加到 %s 个（精读上限 %s）", len(self.loop_tasks), self.analysis_limit)

    async def loop(self, number: int) -> None:
        while not self.stop_event.is_set():
            # 精读已经排满时把这一轮让给轮询/归档，而不是干等着精读腾位置。
            skip = self._skip_kinds()
            try:
                job = self.db.claim_job(self.owner, exclude_kinds=skip)
            except Exception:
                log.exception("领取任务失败")
                await asyncio.sleep(2)
                continue
            if not job:
                await asyncio.sleep(0.35)
                continue
            log.info("执行 %s %s attempt=%s", job["kind"], job["id"], job["attempts"])
            payload = json_loads(job["payload_json"], {}) or {}
            item_id = payload.get("item_id")
            counting = job["kind"] == "analyze_item"
            if counting:
                self._analyze_running += 1
            try:
                await self.runner.handle(job)
                self.db.complete_job(job["id"])
                if item_id:
                    self.db.clear_runtime(f"progress:{item_id}")
                if job["kind"] in {"archive_item", "papers_item", "analyze_item"}:
                    self.db.prune_stale_jobs()
            except QuietHours as exc:
                run_at = (datetime.now(timezone.utc) + timedelta(seconds=exc.delay_seconds)).isoformat(timespec="milliseconds")
                # A scheduled wait must not consume the retry budget or the counter
                # climbs into the hundreds while the job is simply waiting for 20:30.
                self.db.reschedule_job(job["id"], run_at, reason=str(exc), reset_attempts=True)
            except DailyLimitReached as exc:
                # 额度用完不是「这篇坏了」。顺延到次日零点之后，不扣重试次数、
                # 不改条目状态；否则一场额度用尽就会把当天剩余的真文章全判成 error。
                if item_id:
                    self.db.clear_runtime(f"progress:{item_id}")
                self.db.reschedule_job(job["id"], exc.retry_at, reason=str(exc), reset_attempts=True)
                log.info("精读额度已满，%s 顺延到 %s", job["id"], exc.retry_at)
            except WechatNeedsVerification as exc:
                # 验证页不是「文章坏了」，是这一刻被限流。普通重试次数是给网络抖动用的，
                # 不能烧在这上面；但也不能无限等，所以自己数「第几轮」，三轮收尾。
                previous = str(job.get("last_error") or "")
                seen = 0
                if previous.startswith("微信验证 #"):
                    try:
                        seen = int(previous[len("微信验证 #"):].split("：", 1)[0].strip())
                    except ValueError:
                        seen = 0
                seen += 1
                if item_id:
                    self.db.clear_runtime(f"progress:{item_id}")
                if seen > 3:
                    self.db.retry_job(job, f"{type(exc).__name__}: {exc}", force_dead=True)
                    if item_id:
                        self.db.fail_item(
                            item_id,
                            "微信连续 3 轮人机验证拦截，正文还是没拿到；链接和标题已保留，"
                            "风控放开后可以在资料库里重新归档",
                            status="insufficient")
                else:
                    minutes = (30, 90, 180)[min(seen - 1, 2)]
                    run_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
                    self.db.reschedule_job(job["id"], run_at, reason=f"微信验证 #{seen}：{exc}",
                                           reset_attempts=True)
            except Exception as exc:
                log.exception("任务失败 %s", job["id"])
                text = f"{type(exc).__name__}: {exc}"
                # A timed-out reading will not get faster on the fourth try; report it instead of
                # leaving the item spinning in "精读中" for another ten minutes.
                give_up = job["kind"] == "analyze_item" and int(job["attempts"]) >= 2 and (
                    isinstance(exc, TimeoutError) or "request_invalid_parameter" in text)
                if job["kind"] == "poll_source" and job_error_is_environmental(text):
                    # 采集失败不是"这个任务坏了"，是微信没登录/通道还没就绪。下一轮
                    # 调度自然会再试；写成 DEAD 只会让失败列表按小时膨胀（本机 786
                    # 条「未能识别当前微信账号」就是这么来的）。来源的 last_error
                    # 已经记下了真正的原因，这里只把任务记录收起来。
                    self.db.execute("""UPDATE jobs SET state='CANCELLED',last_error=?,owner=NULL,
                        lease_until=NULL,updated_at=? WHERE id=?""", (text[:4000], now_iso(), job["id"]))
                    state = "CANCELLED"
                else:
                    state = self.db.retry_job(job, text, force_dead=give_up)
                if item_id:
                    self.db.clear_runtime(f"progress:{item_id}")
                if state == "DEAD":
                    if job["kind"] in {"archive_item", "papers_item", "analyze_item"} and payload.get("item_id"):
                        self.db.fail_item(payload["item_id"], f"{job['kind']} 多次失败：{exc}")
                    if job["kind"] == "deliver_batch" and payload.get("batch_id"):
                        self.db.execute("UPDATE notification_batches SET last_error=? WHERE id=?",
                                        (str(exc)[:4000], payload["batch_id"]))
            finally:
                if counting:
                    self._analyze_running -= 1
            await asyncio.sleep(0)

    async def run(self) -> None:
        activated = activate_pending_sources(self.db)
        if activated:
            log.info("新版本首次启动，自动启用 %s 个本地微信来源", activated)
        cleaned = cleanup_existing_items(self.db)
        if cleaned["excluded"]:
            log.info("启动科研复核：移出 %s 条非目标内容", cleaned["excluded"])
        if cleaned["restored"]:
            log.info("启动科研复核：恢复 %s 条此前被误过滤的资料", cleaned["restored"])
        duplicates = dedupe_duplicate_items(self.db)
        if duplicates:
            log.info("重复链接整理：合并隐藏 %s 条重复副本", duplicates)
        marketing = cleanup_marketing_items(self.db)
        if marketing:
            log.info("正文复核：%s 条引流软文已移出资料库（可在「已过滤」里恢复）", marketing)
        requeued = requeue_limit_blocked(self.db)
        if requeued:
            log.info("额度用尽时被误判失败的 %s 条已放回精读队列", requeued)
        recovered = self.db.recover_expired_jobs()
        pruned = self.db.prune_stale_jobs()
        if pruned:
            log.info("已清理 %s 条过期的失败任务记录", pruned)
        pruned_batches = self.db.prune_notification_history()
        if pruned_batches:
            log.info("已清理 %s 条过期汇报记录", pruned_batches)
        # 上一次退出时 sqlite 会顺手删掉 radar.db-shm，本机的安全删除垫片会把它
        # 塞进回收站；启动时先扫一遍，这些自己造的文件不该在回收站里过夜。
        try:
            from .services import backup as _backup
            swept = _backup.sweep_trash()
            if swept:
                log.info("回收站里本项目的 %s 条记录已彻底清掉", swept)
        except Exception:                           # noqa: BLE001 - 清扫失败不影响启动
            log.exception("回收站清扫失败")
        closed_before = self.db.one("""SELECT COUNT(*) c FROM notification_batches
            WHERE state='CLOSED' AND priority='normal'""")["c"]
        merged_batch = merge_closed_batches(self.db)
        if merged_batch and closed_before > 1:
            log.info("汇报批次已合并为 %s，队列中只保留一条发送任务", merged_batch)
        log.info("Worker 启动 owner=%s recovered=%s 精读上限=%s",
                 self.owner, recovered, self.analysis_limit)
        self.tasks = [asyncio.create_task(self.scheduler()), asyncio.create_task(self.heartbeat()),
                      asyncio.create_task(self.jsonl_watcher()), asyncio.create_task(self.backup_loop()),
                      asyncio.create_task(self.account_sync_loop()),
                      asyncio.create_task(self.wechat_watcher()),
                      asyncio.create_task(self.settings_watch()),
                      asyncio.create_task(self.budget_watch())]
        self._ensure_loops(self._desired_loops())
        await self.stop_event.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def main() -> None:
    configure_logging("worker")
    if not single_instance():
        log.warning("Worker 已在运行，本实例退出")
        return
    worker = Worker()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, worker.stop)
        except (ValueError, OSError):
            pass
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
