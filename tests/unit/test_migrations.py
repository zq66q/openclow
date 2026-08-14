"""Alembic 迁移机制测试 — 验证 schema 版本管理可用。

通过 subprocess 调用真实的 `alembic` CLI（与运维命令一致），
用临时数据库验证: 全新库建表 / 旧库平滑接管 / downgrade。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = REPO_ROOT / "src"

_LEGACY_DDL = """
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
CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS business_sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    messages     TEXT NOT NULL DEFAULT '[]',
    metadata     TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bs_user
    ON business_sessions(user_id, updated_at DESC);
"""


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    """在隔离环境下运行 alembic 命令。"""
    env = dict(os.environ)
    env["ALEMBIC_DB_PATH"] = str(db_path)
    env["PYTHONPATH"] = str(_SRC_DIR)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _indexes(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()


def test_upgrade_head_creates_full_schema(tmp_path) -> None:
    """全新库 upgrade head 后应包含全部业务表和索引。"""
    db = tmp_path / "memory.db"
    result = _run_alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    tables = _tables(db)
    indexes = _indexes(db)
    assert {"memories", "business_sessions", "_schema_version", "alembic_version"} <= tables
    assert {"idx_memories_user", "idx_memories_importance", "idx_bs_user"} <= indexes


def test_upgrade_on_existing_legacy_db_keeps_data(tmp_path) -> None:
    """已由应用建表的旧库（无 alembic_version）应平滑接管且不丢数据。"""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_LEGACY_DDL)
        conn.execute("INSERT OR IGNORE INTO _schema_version (version) VALUES (1)")
        conn.execute(
            "INSERT INTO memories (memory_id, user_id, key, value, created_at, updated_at)"
            " VALUES ('m1', 'u1', 'k', 'v', 1, 1)"
        )
        conn.execute(
            "INSERT INTO business_sessions (session_id, user_id, created_at, updated_at) VALUES ('s1', 'u1', 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()

    result = _run_alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT memory_id FROM memories").fetchall() == [("m1",)]
        assert conn.execute("SELECT session_id FROM business_sessions").fetchall() == [("s1",)]
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] is not None
    finally:
        conn.close()


def test_downgrade_base_removes_business_tables(tmp_path) -> None:
    """downgrade base 后业务表应被删除（alembic_version 保留）。"""
    db = tmp_path / "memory.db"
    up = _run_alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _run_alembic(db, "downgrade", "base")
    assert down.returncode == 0, down.stderr

    tables = _tables(db)
    assert {"memories", "business_sessions", "_schema_version"}.isdisjoint(tables)
    assert "alembic_version" in tables


def test_current_reports_head_revision(tmp_path) -> None:
    """`alembic current` 应报告已升级到 head 版本。"""
    db = tmp_path / "memory.db"
    up = _run_alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr

    result = _run_alembic(db, "current")
    assert result.returncode == 0, result.stderr
    assert "be41079bf6fb" in result.stdout
