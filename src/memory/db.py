#SQLite 结构化存储

"""记忆系统 SQLite 数据库 — 连接管理 + Schema 初始化。

从 long_term.py 拆出，统一管理所有记忆相关的 SQLite 表，
避免多处重复建表逻辑。线程安全。
"""

from __future__ import annotations

import os
import sqlite3
from threading import Lock

from core.logger import logger
from core.settings import settings

# ---------------------------------------------------------------------------
# Schema 版本
# ---------------------------------------------------------------------------

DB_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id   TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'fact',
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    metadata    TEXT DEFAULT '{}',
    importance  REAL DEFAULT 0.5,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_user
    ON memories(user_id, memory_type);

CREATE INDEX IF NOT EXISTS idx_memories_importance
    ON memories(user_id, importance DESC);

CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY
);
"""

# ---------------------------------------------------------------------------
# MemoryDB
# ---------------------------------------------------------------------------


class MemoryDB:
    """SQLite 连接管理 + 表结构初始化。

    用法:
        db = MemoryDB()                          # 用 settings 默认路径
        db = MemoryDB(db_path="/data/mem.db")    # 自定义路径

        conn = db.get_conn()
        # ... 读写 ...
        conn.close()

    线程安全（Lock 保护建表，连接本身每次新建、调用方负责关闭）。
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.memory.db_path
        self._lock = Lock()
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> str:
        return self._db_path

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    def get_conn(self) -> sqlite3.Connection:
        """获取一个 configured SQLite 连接（WAL + busy_timeout + foreign_keys）。

        调用方用完必须 close()。
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """幂等初始化所有记忆表。"""
        with self._lock, self.get_conn() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO _schema_version (version) VALUES (?)",
                (DB_VERSION,),
            )
            conn.commit()
        logger.debug("memory db schema ready", extra={"db_path": self._db_path, "version": DB_VERSION})
