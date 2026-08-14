"""Alembic 迁移环境 — 面向 OpenClaw 主库 memory.db。

数据库路径解析优先级（从高到低）:
  1. 环境变量 ALEMBIC_DB_PATH —— 测试 / CI 用临时库
  2. 项目配置 settings.memory.db_path —— 生产环境（.env 或环境变量 MEMORY_DB_PATH 覆盖）

相对路径一律锚定到项目根目录，保证从任意 cwd 运行结果一致。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine

from alembic import context

# 项目根目录（env.py 位于 <root>/alembic/env.py）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 确保 src 包可导入（跨平台，不依赖 alembic.ini 的 prepend_sys_path 分隔符）
_SRC_DIR = _PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from core.settings import settings  # noqa: E402  (必须放在 sys.path 调整之后)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 本项目使用原生 sqlite3 + 手写 DDL，无 ORM metadata；迁移脚本用 op.execute() 编写。
target_metadata = None


def _db_url() -> str:
    """解析目标数据库 URL。

    优先级: ALEMBIC_DB_PATH 环境变量 > settings.memory.db_path。
    相对路径相对项目根目录解析。
    """
    env_path = os.environ.get("ALEMBIC_DB_PATH")
    db_path = Path(env_path) if env_path else Path(settings.memory.db_path)
    if not db_path.is_absolute():
        db_path = _PROJECT_ROOT / db_path
    return f"sqlite:///{db_path.as_posix()}"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    # 迁移用独立连接即可；SQLite 场景使用 NullPool 避免连接滞留
    from sqlalchemy import pool as _pool

    connectable = create_engine(_db_url(), poolclass=_pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
