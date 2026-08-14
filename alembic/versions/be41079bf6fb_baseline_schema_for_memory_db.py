"""baseline schema for memory.db

Revision ID: be41079bf6fb
Revises:
Create Date: 2026-08-14 17:56:50.938805

OpenClaw 主库（memory.db）初始 schema，覆盖:
  - memories + 索引            (src/memory/db.py)
  - business_sessions + 索引    (src/business/session.py)
  - _schema_version            (应用内置版本标记，与 alembic_version 并存)

注意: 使用 IF NOT EXISTS 的幂等 DDL，对"已有旧库但无 alembic_version 表"
的部署也能平滑接管，无需先手工 drop 数据。
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "be41079bf6fb"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASELINE_DDL = """
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


def upgrade() -> None:
    """Upgrade schema."""
    # op.execute 只允许单条语句；多语句 DDL 走底层 sqlite3 executescript
    raw = op.get_bind().connection
    raw.executescript(_BASELINE_DDL)
    # 与 src/memory/db.py 的 _init_schema() 保持一致的应用版本标记
    op.execute("INSERT OR IGNORE INTO _schema_version (version) VALUES (1)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS business_sessions")
    op.execute("DROP TABLE IF EXISTS memories")
    op.execute("DROP TABLE IF EXISTS _schema_version")
