# -*- coding: utf-8 -*-
"""SQLite 单文件数据库：单例、懒加载、WAL + busy_timeout、写重试、事务上下文、建表 DDL。

路径解析（ARCHITECTURE_V2.md §1.1）：
1. 环境变量 `APP_DATA_DIR` 优先（便于测试注入临时目录）；
2. 探测 `/mnt/workspace/data`（ModelScope 持久化目录，容器重启不丢）；
3. 回退到项目本地 `./data/app.db`。

测试用：调用 `set_test_db_path(path)` 在 import 任何 DAO 之前切换数据库（如 ":memory:" 或临时文件）。
"""
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# 模块级可切换的数据库路径（测试注入用）；None = 走生产路径解析。
_TEST_PATH = None

# 持久化根候选（ModelScope 容器持久化目录）
_PROD_DIR = Path("/mnt/workspace/data")
# 项目本地回退目录（仓库内 data/）
_LOCAL_DIR = Path(__file__).resolve().parents[1] / "data"

# 全局写锁：所有事务经此锁串行化，杜绝并发双花（ARCHITECTURE_V2.md §10）。
_write_lock = threading.Lock()


def set_test_db_path(path) -> None:
    """测试注入：切换 DB 路径并复位单例。path=None 恢复生产解析。"""
    global _TEST_PATH
    _TEST_PATH = path
    Database._instance = None


def _resolve_db_path() -> Path:
    if _TEST_PATH is not None:
        return Path(_TEST_PATH)
    env = os.environ.get("APP_DATA_DIR")
    if env:
        return Path(env) / "app.db"
    # 平台只挂载 /mnt/workspace 本体，data/ 子目录需主动创建；
    # 之前探测 /mnt/workspace/data 永远 False → 回退容器临时路径 → 每次部署数据重置（V3 事故）
    ws = Path("/mnt/workspace")
    if ws.is_dir():
        try:
            (ws / "data").mkdir(parents=True, exist_ok=True)
            return ws / "data" / "app.db"
        except OSError:
            pass
    return _LOCAL_DIR / "app.db"


def data_root() -> Path:
    """数据库所在的数据根目录（app.db 的父目录）。隔离层据此拼 users/ 子目录。"""
    return _resolve_db_path().parent


def now_iso() -> str:
    """ISO8601 UTC 字符串（ARCHITECTURE_V2.md §10 约定）。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 建表 DDL（幂等）
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'user',
    status       TEXT NOT NULL DEFAULT 'active',
    credits      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

CREATE TABLE IF NOT EXISTS credit_ledger (
    ledger_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(user_id),
    change        INTEGER NOT NULL,
    type          TEXT NOT NULL,
    task_id       INTEGER,
    balance_after INTEGER NOT NULL,
    operator_id   INTEGER,
    note          TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON credit_ledger(user_id);
CREATE INDEX IF NOT EXISTS idx_ledger_task ON credit_ledger(task_id);
CREATE INDEX IF NOT EXISTS idx_ledger_created ON credit_ledger(created_at);

CREATE TABLE IF NOT EXISTS tasks (
    task_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(user_id),
    product_name    TEXT,
    product_name_en TEXT,
    languages       TEXT NOT NULL,
    platforms       TEXT NOT NULL,
    language_count  INTEGER NOT NULL,
    platform_count  INTEGER NOT NULL,
    compliance_level TEXT,
    status          TEXT NOT NULL,
    credit_cost     INTEGER NOT NULL DEFAULT 0,
    credit_refunded INTEGER NOT NULL DEFAULT 0,
    result_summary  TEXT,
    result_path     TEXT,
    created_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id    INTEGER NOT NULL,
    action         TEXT NOT NULL,
    target_user_id INTEGER,
    detail         TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_operator ON audit_log(operator_id);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_user_id);

CREATE TABLE IF NOT EXISTS session_revocation (
    jti         TEXT PRIMARY KEY,
    expires_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revocation_expires ON session_revocation(expires_at);
"""


class Database:
    """SQLite 单例。连接懒创建（首次 get()），自动开 WAL + busy_timeout 并建表。"""

    _instance = None

    def __init__(self):
        self._path = _resolve_db_path()
        self._conn = None
        self._connect()

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = Database()
        return cls._instance

    @classmethod
    def reset(cls):
        """测试用：关闭并丢弃单例。"""
        if cls._instance is not None:
            try:
                cls._instance._conn.close()
            except Exception:
                pass
            cls._instance = None

    def _connect(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：后台生成线程与主线程共享同一连接，由写锁串行化。
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        try:
            self._conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
        self.init_schema()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ 执行与重试
    def execute(self, sql: str, params: tuple = ()):
        """单条写/读；遇 database is locked 做指数退避重试（最多 5 次）。"""
        last = None
        for attempt in range(5):
            try:
                return self._conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower():
                    last = e
                    if attempt < 4:
                        time.sleep(0.05 * (2 ** attempt))
                        continue
                raise
        if last:
            raise last

    def executescript(self, sql: str):
        return self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def query_all(self, sql: str, params: tuple = ()) -> list:
        cur = self.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        cur = self.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def last_insert_id(self) -> int:
        return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # ------------------------------------------------------------ 事务
    @contextmanager
    def transaction(self):
        """显式事务：BEGIN -> yield -> COMMIT / ROLLBACK，并串行化写（防双花）。"""
        with _write_lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

