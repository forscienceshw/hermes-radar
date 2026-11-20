import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from .paths import DB_PATH
from .utils import canonical_url, humanize_error_text, now_iso, parse_time, stable_json, utcnow

# 库结构版本。加字段/改表时把这个数字 +1，init() 会把新值写进
# PRAGMA user_version，于是"这是哪个版本的库"可以和代码版本对得上。
# 目前只用于诊断：新代码遇到旧库靠下面的补列逻辑自动升级。
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    poll_seconds INTEGER NOT NULL DEFAULT 300,
    next_poll_at TEXT,
    last_poll_at TEXT,
    last_success_at TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wechat_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    account_id TEXT,
    category_code TEXT NOT NULL DEFAULT 'OTHER',
    fit TEXT NOT NULL DEFAULT 'OPTIONAL',
    reason TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wechat_accounts_filters ON wechat_accounts(enabled, category_code, fit);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source_id TEXT,
    kind TEXT NOT NULL,
    external_key TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    category TEXT NOT NULL DEFAULT '待分类',
    tags_json TEXT NOT NULL DEFAULT '[]',
    text TEXT NOT NULL DEFAULT '',
    html_path TEXT,
    raw_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    importance REAL NOT NULL DEFAULT 0,
    relevance REAL NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    analysis_json TEXT NOT NULL DEFAULT '{}',
    analyzed_at TEXT,
    excluded_at TEXT,
    exclusion_reason TEXT,
    FOREIGN KEY(source_id) REFERENCES sources(id),
    UNIQUE(source_id, external_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_global_identity ON items(kind, canonical_url);
CREATE INDEX IF NOT EXISTS idx_items_discovered ON items(discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    doi TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    authors_json TEXT NOT NULL DEFAULT '[]',
    year TEXT NOT NULL DEFAULT '',
    journal TEXT NOT NULL DEFAULT '',
    oa_status TEXT NOT NULL DEFAULT 'unknown',
    pdf_url TEXT,
    landing_url TEXT,
    local_path TEXT,
    text_path TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_papers (
    item_id TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '正文 DOI',
    PRIMARY KEY(item_id, paper_id),
    FOREIGN KEY(item_id) REFERENCES items(id),
    FOREIGN KEY(paper_id) REFERENCES papers(id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'READY',
    priority INTEGER NOT NULL DEFAULT 100,
    run_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    owner TEXT,
    lease_until TEXT,
    last_error TEXT,
    dedupe_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, run_at, priority);
CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(state, lease_until);
CREATE TABLE IF NOT EXISTS ideas (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_batches (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'OPEN',
    priority TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT,
    delivery_id TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL DEFAULT 'inbox'
);
CREATE INDEX IF NOT EXISTS idx_batches_state ON notification_batches(state, deadline);
CREATE TABLE IF NOT EXISTS notification_items (
    item_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id),
    FOREIGN KEY(batch_id) REFERENCES notification_batches(id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    item_id TEXT,
    source_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
CREATE TABLE IF NOT EXISTS runtime (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    item_id UNINDEXED, title, content, summary, tags, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    item_id TEXT,
    section TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_item ON notes(item_id);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(pinned DESC, created_at DESC);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '新对话',
    model TEXT NOT NULL DEFAULT '',
    item_id TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(pinned DESC, updated_at DESC);
CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id, created_at);
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'library',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_collections_name ON collections(scope, name);
CREATE TABLE IF NOT EXISTS collection_entries (
    collection_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(collection_id, entry_type, entry_id),
    FOREIGN KEY(collection_id) REFERENCES collections(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_collection_entries_entry ON collection_entries(entry_type, entry_id);
CREATE TABLE IF NOT EXISTS filter_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL DEFAULT 'account',
    account TEXT NOT NULL DEFAULT '',
    field TEXT NOT NULL DEFAULT 'title',
    patterns TEXT NOT NULL DEFAULT '',
    match_mode TEXT NOT NULL DEFAULT 'any',
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    hits INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filter_rules_enabled ON filter_rules(enabled);
CREATE TABLE IF NOT EXISTS wechat_article_marks (
    canonical_url TEXT PRIMARY KEY,
    account_pk INTEGER NOT NULL DEFAULT 0,
    gh_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'excluded',
    reason TEXT NOT NULL DEFAULT '',
    rule_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wechat_article_marks_account ON wechat_article_marks(account_pk, state);
"""


class Database:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        # WAL is persistent database state. Setting it on every read connection
        # causes needless write transitions and intermittent readonly errors.
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def init(self) -> None:
        conn = self.connect()
        try:
            # 只读检查再决定要不要切：`PRAGMA journal_mode=WAL` 即使已经是 WAL，
            # 在别的连接正在读写时也要抢一把排他锁。服务已经在跑的时候再构造一个
            # Database()（脚本、一次性命令、测试），这一下就可能把**别人**踢进
            # readonly——2026-11-18 收藏按钮连续 500 就是这么来的。
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode or "").lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
            if "excluded_at" not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN excluded_at TEXT")
            if "exclusion_reason" not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN exclusion_reason TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_excluded ON items(excluded_at)")
            # 只升不降：代码回滚到旧版本时不该把库版本改小，否则下次升级会重复跑。
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            conn.close()
        self._migrate_wechat_canonical()

    def schema_info(self) -> dict[str, Any]:
        """给诊断用：库是哪一版、多大、几张表。"""
        conn = self.connect()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            conn.close()
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"file_version": version, "code_version": SCHEMA_VERSION,
                "tables": tables, "size_mb": round(size / 1024 / 1024, 1)}

    def _migrate_wechat_canonical(self) -> None:
        """Collapse the same WeChat article shared with different tracking parameters.

        Runs once per database. Existing rows are only rewritten when the new URL
        is free; otherwise the stale duplicate is left for the dedupe sweep.
        """
        if self.get_runtime("canonical_wechat_v2"):
            return
        rows = self.query("""SELECT id,url,canonical_url FROM items
            WHERE kind='article' AND url LIKE '%mp.weixin.qq.com%'""")
        changes = []
        for row in rows:
            target = canonical_url(row["url"] or "")
            if target and target != row["canonical_url"]:
                changes.append((target, row["id"]))
        changed = 0
        if changes:
            with self.transaction() as conn:
                for target, item_id in changes:
                    try:
                        changed += conn.execute("UPDATE items SET canonical_url=? WHERE id=?",
                                                (target, item_id)).rowcount
                    except sqlite3.IntegrityError:
                        continue
        self.set_runtime("canonical_wechat_v2", now_iso())
        if changed:
            self.add_event("canonical_migrated", f"规范化 {changed} 条微信链接，后续分享参数不再产生重复条目")

    def _repair_journal_mode(self) -> bool:
        """把库拉回 WAL。只在 readonly 重试用尽一半时才调用。"""
        try:
            conn = self.connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            finally:
                conn.close()
            return True
        except sqlite3.Error:
            return False

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = None
            begun = False
            # This Windows/E-drive setup has occasionally raised a transient
            # readonly error at BEGIN even while the database remains writable.
            for attempt in range(4):
                conn = self.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    begun = True
                    break
                except sqlite3.OperationalError as exc:
                    conn.close()
                    conn = None
                    if "readonly" not in str(exc).lower() or attempt == 3:
                        raise
                    if attempt >= 1:
                        # journal_mode 被别的进程踢回 delete 时，WAL 的写路径就没了，
                        # 之后每次 BEGIN IMMEDIATE 都报 readonly。重置一次能救回来。
                        self._repair_journal_mode()
                    time.sleep(0.25 * (2 ** attempt))
            try:
                assert conn is not None
                yield conn
                conn.execute("COMMIT")
            except Exception:
                if begun:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                raise
            finally:
                conn.close()

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql + " LIMIT 1", params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount

    def add_event(self, event_type: str, message: str, item_id: str | None = None,
                  source_id: str | None = None, conn: sqlite3.Connection | None = None) -> None:
        values = (event_type, message[:4000], item_id, source_id, now_iso())
        if conn is not None:
            conn.execute("INSERT INTO events(type,message,item_id,source_id,created_at) VALUES(?,?,?,?,?)", values)
        else:
            self.execute("INSERT INTO events(type,message,item_id,source_id,created_at) VALUES(?,?,?,?,?)", values)

    def enqueue_job(self, kind: str, payload: dict[str, Any], run_at: str | None = None,
                    dedupe_key: str | None = None, priority: int = 100, max_attempts: int = 5,
                    conn: sqlite3.Connection | None = None) -> bool:
        job_id = str(uuid.uuid4())
        timestamp = now_iso()
        values = (job_id, kind, stable_json(payload), priority, run_at or timestamp,
                  max_attempts, dedupe_key, timestamp, timestamp)
        sql = """INSERT OR IGNORE INTO jobs
                 (id,kind,payload_json,state,priority,run_at,max_attempts,dedupe_key,created_at,updated_at)
                 VALUES(?,?,?,'READY',?,?,?,?,?,?)"""
        if conn is not None:
            cursor = conn.execute(sql, values)
            return cursor.rowcount > 0
        with self.transaction() as own_conn:
            cursor = own_conn.execute(sql, values)
            return cursor.rowcount > 0

    def discover_item(self, candidate: dict[str, Any]) -> tuple[str, bool]:
        url = candidate["url"].strip()
        canonical = candidate.get("canonical_url") or canonical_url(url)
        kind = candidate.get("kind", "article")
        external_key = candidate.get("external_key") or canonical
        source_id = candidate.get("source_id")
        existing = self.one("SELECT id,status,excluded_at FROM items WHERE kind=? AND canonical_url=?", (kind, canonical))
        if existing:
            if existing["status"] == "discovered" and not existing["excluded_at"] and not candidate.get("defer_processing"):
                self.enqueue_job("archive_item", {"version": 1, "item_id": existing["id"]},
                                 dedupe_key=f"archive:{existing['id']}:initial", priority=70)
            return existing["id"], False
        item_id = str(uuid.uuid4())
        timestamp = now_iso()
        title = (candidate.get("title") or "").strip()
        with self.transaction() as conn:
            duplicate = conn.execute("SELECT id,status,excluded_at FROM items WHERE kind=? AND canonical_url=?", (kind, canonical)).fetchone()
            if duplicate:
                if duplicate["status"] == "discovered" and not duplicate["excluded_at"] and not candidate.get("defer_processing"):
                    self.enqueue_job("archive_item", {"version": 1, "item_id": duplicate["id"]},
                                     dedupe_key=f"archive:{duplicate['id']}:initial", priority=70, conn=conn)
                return duplicate["id"], False
            conn.execute("""INSERT INTO items
                (id,source_id,kind,external_key,url,canonical_url,title,author,published_at,discovered_at,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                item_id, source_id, kind, external_key, url, canonical, title,
                candidate.get("author") or "", candidate.get("published_at"), timestamp,
                stable_json(candidate.get("metadata") or {}),
            ))
            conn.execute("INSERT INTO search_index(item_id,title,content,summary,tags) VALUES(?,?,?,?,?)",
                         (item_id, title, "", "", ""))
            if not candidate.get("defer_processing"):
                self.enqueue_job("archive_item", {"version": 1, "item_id": item_id},
                                 dedupe_key=f"archive:{item_id}:initial", priority=70, conn=conn)
            self.add_event("indexed" if candidate.get("defer_processing") else "discovered",
                           f"收录历史索引：{title or canonical}" if candidate.get("defer_processing") else f"发现：{title or canonical}",
                           item_id, source_id, conn)
        return item_id, True

    def set_items_excluded(self, item_ids: list[str], reason: str, excluded: bool = True,
                           manual: bool = False) -> int:
        """Hide items reversibly without deleting their archived evidence.

        ``manual=True`` 表示这是用户自己点出来的决定（补录、恢复收录）。这类条目会在
        ``metadata.manual_keep`` 上留个印子，下次启动的自动复核不再拿词表去扫它——
        否则今天补录、明天重启又被「标题和简介未显示科研相关性」扫走，用户只会觉得
        补录没生效。
        """
        ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        if not ids:
            return 0
        timestamp = now_iso()
        changed = 0
        with self.transaction() as conn:
            for start in range(0, len(ids), 300):
                chunk = ids[start:start + 300]
                marks = ",".join("?" for _ in chunk)
                if excluded:
                    cursor = conn.execute(f"""UPDATE items SET excluded_at=?,exclusion_reason=?
                        WHERE id IN ({marks}) AND excluded_at IS NULL""", [timestamp, reason[:500], *chunk])
                    conn.execute(f"""UPDATE jobs SET state='CANCELLED',owner=NULL,lease_until=NULL,updated_at=?
                        WHERE state IN ('READY','RETRY_WAIT','LEASED')
                        AND json_extract(payload_json,'$.item_id') IN ({marks})""", [timestamp, *chunk])
                    conn.execute(f"""DELETE FROM notification_items WHERE item_id IN ({marks})
                        AND batch_id IN (SELECT id FROM notification_batches WHERE state='OPEN')""", chunk)
                else:
                    cursor = conn.execute(f"""UPDATE items SET excluded_at=NULL,exclusion_reason=NULL
                        WHERE id IN ({marks}) AND excluded_at IS NOT NULL""", chunk)
                    if manual:
                        conn.execute(f"""UPDATE items SET metadata_json=json_set(
                            CASE WHEN COALESCE(metadata_json,'') IN ('','null') THEN '{{}}' ELSE metadata_json END,
                            '$.manual_keep',1) WHERE id IN ({marks})""", chunk)
                changed += cursor.rowcount
            self.add_event("library_excluded" if excluded else "library_restored",
                           f"{'移出主资料库' if excluded else '恢复'} {changed} 条；原因：{reason}", conn=conn)
        return changed

    def update_archive(self, item_id: str, data: dict[str, Any]) -> None:
        tags = data.get("tags") or []
        with self.transaction() as conn:
            existing = conn.execute("SELECT metadata_json FROM items WHERE id=?", (item_id,)).fetchone()
            previous_metadata = json.loads(existing["metadata_json"]) if existing and existing["metadata_json"] else {}
            merged_metadata = {**previous_metadata, **(data.get("metadata") or {})}
            updated = conn.execute("""UPDATE items SET title=?,author=?,published_at=COALESCE(?,published_at),
                text=?,html_path=?,raw_path=?,metadata_json=?,status='archived',error=NULL
                WHERE id=? AND excluded_at IS NULL""", (
                data.get("title") or "未命名", data.get("author") or "", data.get("published_at"),
                data.get("text") or "", data.get("html_path"), data.get("raw_path"),
                stable_json(merged_metadata), item_id,
            ))
            # Excluded rows are never rewritten by a job that was already running.
            if updated.rowcount != 1:
                return
            conn.execute("DELETE FROM search_index WHERE item_id=?", (item_id,))
            conn.execute("INSERT INTO search_index(item_id,title,content,summary,tags) VALUES(?,?,?,?,?)",
                         (item_id, data.get("title") or "", data.get("text") or "", "", " ".join(tags)))
            self.add_event("archived", f"已归档：{data.get('title') or item_id}", item_id, conn=conn)

    def save_analysis(self, item_id: str, analysis: dict[str, Any], status: str = "ready") -> None:
        tags = analysis.get("tags") or []
        with self.transaction() as conn:
            updated = conn.execute("""UPDATE items SET status=?,category=?,tags_json=?,importance=?,relevance=?,
                summary=?,analysis_json=?,analyzed_at=?,error=NULL WHERE id=? AND excluded_at IS NULL""", (
                status, analysis.get("category") or "综合科研", stable_json(tags),
                float(analysis.get("importance_score") or 0), float(analysis.get("relevance_score") or 0),
                analysis.get("summary") or analysis.get("one_sentence") or "", stable_json(analysis),
                now_iso(), item_id,
            ))
            if updated.rowcount != 1:
                return
            item = conn.execute("SELECT title,text FROM items WHERE id=?", (item_id,)).fetchone()
            conn.execute("DELETE FROM search_index WHERE item_id=?", (item_id,))
            conn.execute("INSERT INTO search_index(item_id,title,content,summary,tags) VALUES(?,?,?,?,?)", (
                item_id, item["title"] if item else "", item["text"] if item else "",
                analysis.get("summary") or analysis.get("one_sentence") or "", " ".join(tags),
            ))
            self.add_event("analyzed", f"分析完成，重要度 {analysis.get('importance_score', 0)}", item_id, conn=conn)

    def fail_item(self, item_id: str, message: str, status: str = "error") -> None:
        row = self.one("SELECT status,summary FROM items WHERE id=?", (item_id,))
        if row and row["status"] in {"ready", "filtered"} and (row["summary"] or "").strip():
            # A failed re-run must never destroy an earlier successful reading.
            self.add_event("item_error", f"重新处理失败，已保留上次精读结果：{message}", item_id)
            return
        # 兜底：任何一整页 HTML 的报错都先压成一句话，资料库里不存网页源码。
        message = humanize_error_text(message)
        self.execute("UPDATE items SET status=?,error=? WHERE id=?", (status, message[:4000], item_id))
        self.add_event("item_error", message, item_id)

    def claim_job(self, owner: str, lease_seconds: int = 1800,
                  exclude_kinds: tuple[str, ...] = ()) -> dict[str, Any] | None:
        # A cloud reading can legitimately run for several minutes; the lease must outlast it.
        timestamp = now_iso()
        lease = utcnow().timestamp() + lease_seconds
        from datetime import datetime, timezone
        lease_iso = datetime.fromtimestamp(lease, timezone.utc).isoformat(timespec="milliseconds")
        # exclude_kinds 让「精读已经排满」时把槽位让给轮询/归档，而不是干等着。
        # 没有它的话，prio 更靠前的精读任务会把后面的任务全堵住。
        skip = ""
        extra: tuple[Any, ...] = ()
        if exclude_kinds:
            skip = f" AND kind NOT IN ({','.join('?' * len(exclude_kinds))})"
            extra = tuple(exclude_kinds)
        with self.transaction() as conn:
            row = conn.execute(f"""SELECT * FROM jobs WHERE state IN ('READY','RETRY_WAIT') AND run_at<=?
                {skip} ORDER BY priority,run_at LIMIT 1""", (timestamp, *extra)).fetchone()
            if not row:
                return None
            updated = conn.execute("""UPDATE jobs SET state='LEASED',owner=?,lease_until=?,attempts=attempts+1,
                updated_at=? WHERE id=? AND state IN ('READY','RETRY_WAIT')""",
                (owner, lease_iso, timestamp, row["id"]))
            if updated.rowcount != 1:
                return None
            job = dict(row)
            job["state"] = "LEASED"
            job["attempts"] += 1
            return job

    def complete_job(self, job_id: str) -> None:
        self.execute("UPDATE jobs SET state='SUCCEEDED',owner=NULL,lease_until=NULL,last_error=NULL,updated_at=? WHERE id=?",
                     (now_iso(), job_id))

    def retry_job(self, job: dict[str, Any], error: str, delay_seconds: int | None = None,
                  force_dead: bool = False) -> str:
        attempts = int(job["attempts"])
        dead = force_dead or attempts >= int(job["max_attempts"])
        delay = delay_seconds if delay_seconds is not None else min(3600, 2 ** min(attempts, 10) * 5)
        from datetime import datetime, timedelta, timezone
        run_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
        state = "DEAD" if dead else "RETRY_WAIT"
        self.execute("UPDATE jobs SET state=?,run_at=?,owner=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
                     (state, run_at, error[:4000], now_iso(), job["id"]))
        return state

    def reschedule_job(self, job_id: str, run_at: str, reason: str = "",
                       reset_attempts: bool = False) -> None:
        """Park a job until `run_at` without treating the wait as a failure.

        Quiet hours and send windows are normal scheduling, not errors: counting them
        as retries made a single digest job climb past 900 "attempts" while it was
        merely waiting for the evening window.
        """
        sql = """UPDATE jobs SET state='RETRY_WAIT',run_at=?,owner=NULL,lease_until=NULL,updated_at=?"""
        args: list[Any] = [run_at, now_iso()]
        if reason:
            sql += ",last_error=?"
            args.append(reason[:500])
        if reset_attempts:
            sql += ",attempts=0"
        sql += " WHERE id=?"
        args.append(job_id)
        self.execute(sql, args)

    def recover_expired_jobs(self) -> int:
        timestamp = now_iso()
        with self.transaction() as conn:
            expired = conn.execute("SELECT id,attempts,max_attempts FROM jobs WHERE state='LEASED' AND lease_until<?",
                                   (timestamp,)).fetchall()
            count = 0
            for row in expired:
                state = "RETRY_WAIT" if row["attempts"] < row["max_attempts"] else "DEAD"
                conn.execute("UPDATE jobs SET state=?,owner=NULL,lease_until=NULL,last_error='租约到期，已自动恢复',updated_at=? WHERE id=?",
                             (state, timestamp, row["id"]))
                count += 1
            # A restarted worker should not wait 15 minutes for leases held by a dead process.
            orphaned = conn.execute("""SELECT id,owner,attempts,max_attempts FROM jobs
                WHERE state='LEASED' AND lease_until>=?""", (timestamp,)).fetchall()
            for row in orphaned:
                if self._owner_alive(row["owner"]):
                    continue
                state = "RETRY_WAIT" if row["attempts"] < row["max_attempts"] else "DEAD"
                conn.execute("UPDATE jobs SET state=?,owner=NULL,lease_until=NULL,last_error='执行进程已退出，任务已自动恢复',updated_at=? WHERE id=?",
                             (state, timestamp, row["id"]))
                count += 1
            return count

    @staticmethod
    def _owner_alive(owner: str | None) -> bool:
        """Owners are 'hostname:pid:nonce'; treat unreadable or foreign owners as alive."""
        if not owner:
            return False
        parts = str(owner).split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            return True
        import socket
        if parts[0] != socket.gethostname():
            return True
        try:
            import psutil
            return psutil.Process(int(parts[1])).is_running()
        except Exception:
            return False

    def heartbeat(self, name: str) -> None:
        timestamp = now_iso()
        self.execute("INSERT INTO runtime(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                     (name, timestamp, timestamp))

    def set_runtime(self, key: str, value: str) -> None:
        timestamp = now_iso()
        self.execute("INSERT INTO runtime(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                     (key, value[:4000], timestamp))

    def get_runtime(self, key: str) -> str | None:
        row = self.one("SELECT value FROM runtime WHERE key=?", (key,))
        return row["value"] if row else None

    def clear_runtime(self, key: str) -> None:
        self.execute("DELETE FROM runtime WHERE key=?", (key,))

    def prune_stale_jobs(self) -> int:
        """Drop failure rows that no longer describe anything the user can act on.

        A job that failed before the item was read successfully is history, not a
        task. Keeping every dead row made the failure tab grow without bound.
        """
        removed = 0
        with self.transaction() as conn:
            cursor = conn.execute("""DELETE FROM jobs WHERE state='DEAD'
                AND kind IN ('archive_item','papers_item','analyze_item')
                AND json_extract(payload_json,'$.item_id') IN (
                    SELECT id FROM items WHERE status IN ('ready','filtered','insufficient') AND analyzed_at IS NOT NULL)""")
            removed += cursor.rowcount
            cursor = conn.execute("""DELETE FROM jobs WHERE state='DEAD'
                AND kind IN ('archive_item','papers_item')
                AND json_extract(payload_json,'$.item_id') IN (
                    SELECT id FROM items WHERE status NOT IN ('discovered','error'))""")
            removed += cursor.rowcount
            cursor = conn.execute("""DELETE FROM jobs WHERE state='DEAD'
                AND json_extract(payload_json,'$.item_id') IN (
                    SELECT id FROM items WHERE excluded_at IS NOT NULL)""")
            removed += cursor.rowcount
            # The paid DataX fallback was removed; its failure rows describe a pipeline that no longer exists.
            cursor = conn.execute("DELETE FROM jobs WHERE state='DEAD' AND last_error LIKE '%dataxapi%'")
            removed += cursor.rowcount
            # 采集任务每轮都有新的 dedupe_key（带时间窗），失败一次就留一条死信：一天
            # 288 条，三天就能把失败列表刷到 800+。同一个来源只留最新一条作为「它
            # 出过问题」的证据，其余都是噪音——真正的原因在 sources.last_error 里。
            cursor = conn.execute("""DELETE FROM jobs WHERE state='DEAD' AND kind='poll_source'
                AND id NOT IN (
                    SELECT MAX(id) FROM jobs WHERE state='DEAD' AND kind='poll_source'
                    GROUP BY json_extract(payload_json,'$.source_id'))""")
            removed += cursor.rowcount
            # A flush job whose batch is already closed, merged or sent has nothing to do.
            cursor = conn.execute("""UPDATE jobs SET state='CANCELLED',owner=NULL,lease_until=NULL,updated_at=?
                WHERE kind='flush_batch' AND state IN ('READY','RETRY_WAIT')
                AND json_extract(payload_json,'$.batch_id') IN (
                    SELECT id FROM notification_batches WHERE state<>'OPEN')""", (now_iso(),))
            removed += cursor.rowcount
            cursor = conn.execute("DELETE FROM jobs WHERE state='DEAD' AND updated_at<?",
                                  ((utcnow() - timedelta(days=30)).isoformat(timespec="milliseconds"),))
            removed += cursor.rowcount
            cursor = conn.execute("""DELETE FROM jobs WHERE state='CANCELLED' AND updated_at<?""",
                                  ((utcnow() - timedelta(days=7)).isoformat(timespec="milliseconds"),))
            removed += cursor.rowcount
            # Delivery jobs are one-shot history once they finish; keeping every row made
            # the task drawer look like a backlog when nothing was actually pending.
            cursor = conn.execute("""DELETE FROM jobs WHERE state IN ('SUCCEEDED','CANCELLED')
                AND kind IN ('deliver_batch','flush_batch') AND updated_at<?""",
                                  ((utcnow() - timedelta(days=14)).isoformat(timespec="milliseconds"),))
            removed += cursor.rowcount
            # A merged batch never delivers on its own, so its cancelled delivery job
            # describes nothing.  Left in place it showed up in the task drawer as a
            # 900-attempt zombie and looked exactly like the retry storm it came from.
            cursor = conn.execute("""DELETE FROM jobs WHERE state='CANCELLED'
                AND kind IN ('deliver_batch','flush_batch')
                AND json_extract(payload_json,'$.batch_id') IS NOT NULL
                AND json_extract(payload_json,'$.batch_id') NOT IN (
                    SELECT id FROM notification_batches WHERE state IN ('OPEN','CLOSED'))""")
            removed += cursor.rowcount
        return removed

    def prune_notification_history(self, days: int = 45) -> int:
        """Drop old sent/merged digest records together with their item links."""
        cutoff = (utcnow() - timedelta(days=max(7, days))).isoformat(timespec="milliseconds")
        removed = 0
        with self.transaction() as conn:
            rows = conn.execute("""SELECT id FROM notification_batches
                WHERE state IN ('SENT','MERGED') AND COALESCE(sent_at,created_at)<? LIMIT 500""",
                                (cutoff,)).fetchall()
            ids = [row["id"] for row in rows]
            for start in range(0, len(ids), 200):
                chunk = ids[start:start + 200]
                marks = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM notification_items WHERE batch_id IN ({marks})", chunk)
                cursor = conn.execute(f"DELETE FROM notification_batches WHERE id IN ({marks})", chunk)
                removed += cursor.rowcount
        return removed

    def source_due(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query("""SELECT * FROM sources WHERE enabled=1 AND type!='jsonl'
            AND (next_poll_at IS NULL OR next_poll_at<=?)
            ORDER BY COALESCE(next_poll_at,created_at) LIMIT ?""", (now_iso(), limit))

    @staticmethod
    def search_terms(query: str) -> list[str]:
        return [term for term in re.split(r"[\s,，、;；|]+", (query or "").strip()) if term][:8]

    def _search_where(self, query: str = "", kind: str = "", status: str = "",
                      since: str | None = None, until: str | None = None,
                      collection: str | None = None, include_excluded: bool = False,
                      use_fts: bool = True, hide_status: str = "") -> tuple[str, list[Any]]:
        """Build one shared WHERE clause for both the page and its total count.

        A term is matched in the title, summary, tags and archived text. Short
        Chinese queries (two characters) cannot use the trigram index at all, so
        LIKE is always part of the clause; FTS is an extra net for 3+ characters.
        """
        conditions = [] if include_excluded else ["i.excluded_at IS NULL"]
        params: list[Any] = []
        if kind:
            conditions.append("i.kind=?")
            params.append(kind)
        if status:
            conditions.append("i.status=?")
            params.append(status)
        if hide_status and status != hide_status:
            # 低相关条目默认不占资料库的位置，但仍可通过状态筛选单独查看。
            conditions.append("i.status<>?")
            params.append(hide_status)
        if since:
            conditions.append("COALESCE(i.published_at,i.discovered_at)>=?")
            params.append(since)
        if until:
            conditions.append("COALESCE(i.published_at,i.discovered_at)<=?")
            params.append(until)
        if collection == "__none__":
            conditions.append("""NOT EXISTS (SELECT 1 FROM collection_entries ce
                WHERE ce.entry_type='item' AND ce.entry_id=i.id)""")
        elif collection:
            conditions.append("""i.id IN (SELECT ce.entry_id FROM collection_entries ce
                WHERE ce.collection_id=? AND ce.entry_type='item')""")
            params.append(collection)
        for term in self.search_terms(query):
            like = f"%{term}%"
            clause = "(i.title LIKE ? OR i.summary LIKE ? OR i.tags_json LIKE ? OR i.text LIKE ?"
            params.extend([like, like, like, like])
            if use_fts and len(term) >= 3:
                clause += " OR i.id IN (SELECT item_id FROM search_index WHERE search_index MATCH ?)"
                params.append('"' + term.replace('"', '""') + '"')
            conditions.append(clause + ")")
        return ("WHERE " + " AND ".join(conditions) if conditions else ""), params

    def search(self, query: str = "", kind: str = "", status: str = "", limit: int = 50,
               offset: int = 0, since: str | None = None, until: str | None = None,
               collection: str | None = None, include_excluded: bool = False,
               hide_status: str = "") -> list[dict[str, Any]]:
        terms = self.search_terms(query)
        select = f"""SELECT i.id,i.kind,i.url,i.title,i.author,i.published_at,i.discovered_at,i.status,
            i.category,i.tags_json,i.importance,i.relevance,i.summary,i.error,i.excluded_at,i.exclusion_reason,
            json_extract(i.metadata_json,'$.processing_policy') AS processing_policy,
            json_extract(i.analysis_json,'$.analysis_mode') AS analysis_mode FROM items i {{where}}
            ORDER BY {{boost}}i.discovered_at DESC LIMIT ? OFFSET ?"""
        boost = "CASE WHEN i.title LIKE ? THEN 0 ELSE 1 END," if terms else ""
        for use_fts in (True, False):
            where, params = self._search_where(query, kind, status, since, until, collection,
                                               include_excluded, use_fts, hide_status)
            args: list[Any] = list(params)
            if terms:
                args.append(f"%{terms[0]}%")
            args.extend([limit, offset])
            try:
                return self.query(select.format(where=where, boost=boost), args)
            except sqlite3.OperationalError:
                if not use_fts:
                    raise
        return []

    def search_count(self, query: str = "", kind: str = "", status: str = "",
                     since: str | None = None, until: str | None = None,
                     collection: str | None = None, include_excluded: bool = False,
                     hide_status: str = "") -> int:
        for use_fts in (True, False):
            where, params = self._search_where(query, kind, status, since, until, collection,
                                               include_excluded, use_fts, hide_status)
            try:
                row = self.one(f"SELECT COUNT(*) c FROM items i {where}", params)
                return int(row["c"]) if row else 0
            except sqlite3.OperationalError:
                if not use_fts:
                    raise
        return 0

    # ---------------- 收藏夹 ----------------
    def collections(self, scope: str = "") -> list[dict[str, Any]]:
        where, params = ("WHERE scope=?", [scope]) if scope else ("", [])
        return self.query(f"""SELECT c.*, (SELECT COUNT(*) FROM collection_entries e
            WHERE e.collection_id=c.id) AS count FROM collections c {where}
            ORDER BY c.created_at""", params)

    def collection_create(self, name: str, scope: str = "library") -> dict[str, Any]:
        name = (name or "").strip()[:60]
        if not name:
            raise ValueError("收藏夹名称不能为空")
        existing = self.one("SELECT * FROM collections WHERE scope=? AND name=?", (scope, name))
        if existing:
            return existing
        collection_id = uuid.uuid4().hex
        timestamp = now_iso()
        self.execute("INSERT INTO collections(id,name,scope,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (collection_id, name, scope, timestamp, timestamp))
        return {"id": collection_id, "name": name, "scope": scope, "count": 0,
                "created_at": timestamp, "updated_at": timestamp}

    def collection_rename(self, collection_id: str, name: str) -> None:
        name = (name or "").strip()[:60]
        if not name:
            raise ValueError("收藏夹名称不能为空")
        self.execute("UPDATE collections SET name=?,updated_at=? WHERE id=?", (name, now_iso(), collection_id))

    def collection_delete(self, collection_id: str) -> int:
        with self.transaction() as conn:
            conn.execute("DELETE FROM collection_entries WHERE collection_id=?", (collection_id,))
            return conn.execute("DELETE FROM collections WHERE id=?", (collection_id,)).rowcount

    def collection_toggle(self, collection_id: str, entry_type: str, entry_id: str,
                          member: bool | None = None) -> dict[str, Any]:
        if not self.one("SELECT id FROM collections WHERE id=?", (collection_id,)):
            raise ValueError("收藏夹不存在")
        current = self.one("""SELECT 1 FROM collection_entries
            WHERE collection_id=? AND entry_type=? AND entry_id=?""", (collection_id, entry_type, entry_id))
        want = (not current) if member is None else bool(member)
        with self.transaction() as conn:
            if want:
                conn.execute("""INSERT OR IGNORE INTO collection_entries
                    (collection_id,entry_type,entry_id,created_at) VALUES(?,?,?,?)""",
                    (collection_id, entry_type, entry_id, now_iso()))
            else:
                conn.execute("""DELETE FROM collection_entries
                    WHERE collection_id=? AND entry_type=? AND entry_id=?""",
                    (collection_id, entry_type, entry_id))
        return {"collection_id": collection_id, "entry_type": entry_type,
                "entry_id": entry_id, "member": want}

    def collection_ids_for(self, entry_type: str, entry_ids: list[str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {str(x): [] for x in entry_ids}
        ids = [str(x) for x in entry_ids]
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            for row in self.query(f"""SELECT collection_id,entry_id FROM collection_entries
                WHERE entry_type=? AND entry_id IN ({marks})""", [entry_type, *chunk]):
                result.setdefault(row["entry_id"], []).append(row["collection_id"])
        return result

    def collection_entry_ids(self, collection_id: str, entry_type: str) -> list[str]:
        return [row["entry_id"] for row in self.query("""SELECT entry_id FROM collection_entries
            WHERE collection_id=? AND entry_type=? ORDER BY created_at DESC""",
            (collection_id, entry_type))]

    def stats(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, sql in {
            "items": "SELECT COUNT(*) c FROM items WHERE excluded_at IS NULL",
            "articles": "SELECT COUNT(*) c FROM items WHERE kind='article' AND excluded_at IS NULL",
            "videos": "SELECT COUNT(*) c FROM items WHERE kind='video' AND excluded_at IS NULL",
            "ready": "SELECT COUNT(*) c FROM items WHERE status='ready' AND excluded_at IS NULL",
            "errors": "SELECT COUNT(*) c FROM items WHERE status='error' AND excluded_at IS NULL",
            "insufficient": "SELECT COUNT(*) c FROM items WHERE status='insufficient' AND excluded_at IS NULL",
            "excluded": "SELECT COUNT(*) c FROM items WHERE excluded_at IS NOT NULL",
            "papers": "SELECT COUNT(*) c FROM papers",
            "papers_downloaded": "SELECT COUNT(*) c FROM papers WHERE status='downloaded'",
            "papers_needed": "SELECT COUNT(*) c FROM papers WHERE status IN ('closed','failed','not_found')",
            "jobs_pending": "SELECT COUNT(*) c FROM jobs WHERE state IN ('READY','RETRY_WAIT','LEASED')",
            "jobs_dead": "SELECT COUNT(*) c FROM jobs WHERE state='DEAD'",
            "sources": "SELECT COUNT(*) c FROM sources WHERE enabled=1",
            "source_errors": "SELECT COUNT(*) c FROM sources WHERE enabled=1 AND error_count>0",
        }.items():
            result[key] = self.one(sql)["c"]
        result["worker_heartbeat"] = self.one("SELECT value FROM runtime WHERE key='worker'")
        return result


def row_with_json(row: dict[str, Any], *fields: str) -> dict[str, Any]:
    result = dict(row)
    for field in fields:
        target = field.removesuffix("_json")
        result[target] = json.loads(result.get(field) or ("[]" if field.endswith("s_json") else "{}"))
    return result
