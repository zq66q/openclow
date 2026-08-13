"""Agent 状态持久化 — 将 AgentContext / WorkflowStep 保存到磁盘，重启不丢失。

支持两种后端：
  - JSON 文件（轻量、人类可读、适合小型部署）
  - SQLite（适合生产环境、支持并发读）

用法:
    store = StateStore()
    store.save_context(ctx)           # 保存会话上下文
    ctx = store.load_context(session_id)  # 恢复会话上下文
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from core.logger import logger
from core.settings import settings


class StateStore:
    """Agent 状态持久化存储。

    自动选择后端：配置 data/states/ 目录存在 → SQLite，否则 → JSON 文件。
    """

    def __init__(self, store_path: str | None = None) -> None:
        store_path = store_path or getattr(settings, "state_store_path", "./data/states")
        self._base_path = Path(store_path)
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._use_sqlite = True  # 默认 SQLite
        self._init_sqlite()

    # ------------------------------------------------------------------
    # 会话上下文 CRUD
    # ------------------------------------------------------------------

    def save_context(self, ctx: Any, session_id: str | None = None) -> str:
        """保存 AgentContext。返回 session_id。"""
        sid = session_id or getattr(ctx, "session_id", None) or _generate_id()
        data = _serialize_context(ctx)

        if self._use_sqlite:
            self._upsert_context_sqlite(sid, data)
        else:
            self._write_json(f"context_{sid}.json", data)

        logger.debug(f"StateStore: saved context {sid}")
        return sid

    def load_context(self, session_id: str) -> dict[str, Any] | None:
        """加载 AgentContext。"""
        if self._use_sqlite:
            return self._load_context_sqlite(session_id)
        return self._read_json(f"context_{session_id}.json")

    def delete_context(self, session_id: str) -> bool:
        """删除 AgentContext。"""
        if self._use_sqlite:
            return self._delete_context_sqlite(session_id)
        path = self._base_path / f"context_{session_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """列出最近的会话。"""
        if self._use_sqlite:
            return self._list_sessions_sqlite(limit)

        sessions: list[dict[str, Any]] = []
        for f in sorted(self._base_path.glob("context_*.json"), reverse=True):
            data = self._read_json(f.name)
            if data:
                sessions.append(
                    {
                        "session_id": data.get("session_id", f.stem.replace("context_", "")),
                        "created_at": data.get("created_at", 0),
                    }
                )
            if len(sessions) >= limit:
                break
        return sessions

    # ------------------------------------------------------------------
    # 工作流步骤状态
    # ------------------------------------------------------------------

    def save_workflow_step(self, session_id: str, step: Any) -> None:
        """保存单个工作流步骤状态。"""
        data = {
            "session_id": session_id,
            "step_id": getattr(step, "step_id", ""),
            "agent_name": getattr(step, "agent_name", ""),
            "status": getattr(step, "status", "pending").value
            if hasattr(getattr(step, "status", "pending"), "value")
            else str(getattr(step, "status", "pending")),
            "result": getattr(step, "result", {}),
            "started_at": getattr(step, "started_at", 0),
            "finished_at": getattr(step, "finished_at", 0),
            "updated_at": time.time(),
        }

        if self._use_sqlite:
            self._upsert_step_sqlite(data)
        else:
            self._write_json(f"step_{session_id}_{data['step_id']}.json", data)

    def load_workflow_steps(self, session_id: str) -> list[dict[str, Any]]:
        """加载某个会话的所有工作流步骤。"""
        if self._use_sqlite:
            return self._load_steps_sqlite(session_id)

        steps: list[dict[str, Any]] = []
        for f in self._base_path.glob(f"step_{session_id}_*.json"):
            data = self._read_json(f.name)
            if data:
                steps.append(data)
        return sorted(steps, key=lambda s: s.get("updated_at", 0))

    # ------------------------------------------------------------------
    # SQLite 后端
    # ------------------------------------------------------------------

    def _init_sqlite(self) -> None:
        """初始化 SQLite 表结构。"""
        self._db_path = self._base_path / "state_store.db"
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contexts (
                    session_id TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, step_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_session ON workflow_steps(session_id)")
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"StateStore: SQLite init failed, falling back to JSON: {exc}")
            self._use_sqlite = False

    def _upsert_context_sqlite(self, sid: str, data: dict) -> None:
        conn = sqlite3.connect(str(self._db_path))
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO contexts (session_id, data_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sid, json.dumps(data, ensure_ascii=False), data.get("created_at", now), now),
        )
        conn.commit()
        conn.close()

    def _load_context_sqlite(self, sid: str) -> dict | None:
        conn = sqlite3.connect(str(self._db_path))
        row = conn.execute("SELECT data_json FROM contexts WHERE session_id = ?", (sid,)).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None

    def _delete_context_sqlite(self, sid: str) -> bool:
        conn = sqlite3.connect(str(self._db_path))
        cursor = conn.execute("DELETE FROM contexts WHERE session_id = ?", (sid,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def _list_sessions_sqlite(self, limit: int) -> list[dict]:
        conn = sqlite3.connect(str(self._db_path))
        rows = conn.execute(
            "SELECT session_id, created_at FROM contexts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [{"session_id": r[0], "created_at": r[1]} for r in rows]

    def _upsert_step_sqlite(self, data: dict) -> None:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            "INSERT OR REPLACE INTO workflow_steps (session_id, step_id, data_json, updated_at) VALUES (?, ?, ?, ?)",
            (data["session_id"], data["step_id"], json.dumps(data, ensure_ascii=False), data["updated_at"]),
        )
        conn.commit()
        conn.close()

    def _load_steps_sqlite(self, sid: str) -> list[dict]:
        conn = sqlite3.connect(str(self._db_path))
        rows = conn.execute(
            "SELECT data_json FROM workflow_steps WHERE session_id = ? ORDER BY updated_at",
            (sid,),
        ).fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # JSON 文件后端
    # ------------------------------------------------------------------

    def _write_json(self, filename: str, data: dict) -> None:
        path = self._base_path / filename
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_json(self, filename: str) -> dict | None:
        path = self._base_path / filename
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"StateStore: failed to read {filename}: {exc}")
            return None


# ------------------------------------------------------------------
# 序列化辅助
# ------------------------------------------------------------------


def _serialize_context(ctx: Any) -> dict[str, Any]:
    """将 AgentContext 或兼容对象序列化为 dict。"""
    if hasattr(ctx, "snapshot"):
        return ctx.snapshot()

    return {
        "session_id": getattr(ctx, "session_id", _generate_id()),
        "data": getattr(ctx, "data", {}),
        "history": getattr(ctx, "history", []),
        "created_at": getattr(ctx, "created_at", time.time()),
    }


def _generate_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]
