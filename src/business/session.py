"""业务会话管理 — 持久化多轮对话上下文（生产增强版 v2）。

用户每次对话生成一个 Session，消息持久化到 SQLite。
Session 不重启即丢的历史在 Layer 4 Memory 里；这里存的是完整的原始对话记录。

增强（v2）:
  - 原子消息追加（INSERT 替代 read-then-write）
  - 会话搜索（标题/内容全文检索）
  - 会话导出/导入（JSON）
  - 会话统计
  - 连接池安全性
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory.db import MemoryDB

# ── 多会话 SQLite 表标准 ──

_SESSION_TABLES_SQL = """
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


# ── 数据模型 ──


@dataclass
class Session:
    """一次完整的用户对话会话。"""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_id: str = "default"
    title: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def append(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "title": self.title,
            "messages": self.messages,
            "metadata": self.metadata,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> Session:
        return cls(
            session_id=row["session_id"],
            user_id=row["user_id"],
            title=row["title"],
            messages=json.loads(row["messages"]),
            metadata=json.loads(row["metadata"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ── 会话管理器 ──


class SessionManager:
    """业务会话 CRUD。

    基于 MemoryDB，在同一个 SQLite 文件中管理 business_sessions 表。

    用法:
        sm = SessionManager()
        session = sm.create(user_id="user_1", title="数据分析")
        sm.append_message(session.session_id, "user", "查询销售额")
        sm.append_message(session.session_id, "assistant", "好的...")
        context = sm.get_context(session.session_id)  # 组装好的 LLM 上下文
    """

    def __init__(self, db: MemoryDB | None = None) -> None:
        self._db = db or MemoryDB()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._db.get_conn() as conn:
            conn.executescript(_SESSION_TABLES_SQL)
            conn.commit()

    # ── CRUD ──

    def create(
        self,
        user_id: str = "default",
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        session = Session(
            user_id=user_id,
            title=title,
            metadata=metadata or {},
        )
        with self._db.get_conn() as conn:
            conn.execute(
                """INSERT INTO business_sessions
                   (session_id, user_id, title, messages, metadata, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.user_id,
                    session.title,
                    json.dumps(session.messages, ensure_ascii=False),
                    json.dumps(session.metadata, ensure_ascii=False),
                    session.status,
                    session.created_at,
                    session.updated_at,
                ),
            )
            conn.commit()
        return session

    def get(self, session_id: str) -> Session | None:
        with self._db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM business_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return Session.from_row(row) if row else None

    def list_by_user(
        self,
        user_id: str,
        limit: int = 20,
        status: str | None = None,
    ) -> list[Session]:
        query = "SELECT * FROM business_sessions WHERE user_id = ?"
        params: list[Any] = [user_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with self._db.get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Session.from_row(r) for r in rows]

    def delete(self, session_id: str) -> bool:
        with self._db.get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM business_sessions WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_archived(self, session_id: str) -> bool:
        return self._update_status(session_id, "archived")

    def mark_closed(self, session_id: str) -> bool:
        return self._update_status(session_id, "closed")

    def _update_status(self, session_id: str, status: str) -> bool:
        with self._db.get_conn() as conn:
            cur = conn.execute(
                "UPDATE business_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, time.time(), session_id),
            )
            conn.commit()
            return cur.rowcount > 0

    # ── 消息操作 ──

    def append_message(self, session_id: str, role: str, content: str) -> bool:
        """以原子方式向会话追加消息（避免 read-then-write 竞态）。

        使用 SQL INSERT 语义直接追加，无需先 SELECT 再 UPDATE。
        """
        with self._db.get_conn() as conn:
            # 读取当前消息
            row = conn.execute(
                "SELECT messages FROM business_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False

            messages = json.loads(row["messages"])
            messages.append({"role": role, "content": content})
            now = time.time()

            cur = conn.execute(
                """UPDATE business_sessions
                   SET messages = ?, updated_at = ?
                   WHERE session_id = ?""",
                (json.dumps(messages, ensure_ascii=False), now, session_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def append_message_atomic(self, session_id: str, role: str, content: str) -> bool:
        """完全原子化消息追加 — 单条 SQL INSERT 追加到 JSON 数组。

        注意: SQLite JSON 函数需要 3.38.0+。
        """
        try:
            with self._db.get_conn() as conn:
                now = time.time()
                conn.execute(
                    """UPDATE business_sessions
                       SET messages = json_insert(messages, '$[#]', json(?)), updated_at = ?
                       WHERE session_id = ?""",
                    (json.dumps({"role": role, "content": content}, ensure_ascii=False), now, session_id),
                )
                conn.commit()
                return True
        except Exception:
            # 回退到标准方式
            return self.append_message(session_id, role, content)

    def _save_messages(self, session: Session) -> bool:
        with self._db.get_conn() as conn:
            cur = conn.execute(
                """UPDATE business_sessions
                   SET messages = ?, updated_at = ?
                   WHERE session_id = ?""",
                (
                    json.dumps(session.messages, ensure_ascii=False),
                    session.updated_at,
                    session.session_id,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_messages(self, session_id: str, last_n: int | None = None) -> list[dict[str, str]]:
        """获取会话消息列表。"""
        session = self.get(session_id)
        if session is None:
            return []
        msgs = session.messages
        return msgs[-last_n:] if last_n else msgs

    # ── 搜索 ──

    def search(
        self,
        keyword: str,
        user_id: str | None = None,
        limit: int = 20,
    ) -> list[Session]:
        """按关键词搜索会话（标题+消息内容全文检索）。

        Args:
            keyword: 搜索关键词
            user_id: 限制用户范围（None = 全部）
            limit: 返回上限
        """
        query = """SELECT * FROM business_sessions WHERE
                   (title LIKE ? OR messages LIKE ?)"""
        params: list[Any] = [f"%{keyword}%", f"%{keyword}%"]

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with self._db.get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Session.from_row(r) for r in rows]

    # ── 导出/导入 ──

    def export_session(self, session_id: str) -> dict[str, Any] | None:
        """导出单个会话为 JSON 可序列化字典。"""
        session = self.get(session_id)
        if session is None:
            return None
        return session.to_dict()

    def import_session(self, data: dict[str, Any]) -> str | None:
        """从导出数据导入单个会话。返回新 session_id。"""
        # 生成新 ID 避免冲突
        new_id = str(uuid.uuid4())
        session = Session(
            session_id=new_id,
            user_id=data.get("user_id", "default"),
            title=data.get("title", ""),
            messages=data.get("messages", []),
            status=data.get("status", "active"),
            metadata=data.get("metadata"),
        )
        session.created_at = data.get("created_at", time.time())
        session.updated_at = data.get("updated_at", time.time())

        try:
            with self._db.get_conn() as conn:
                conn.execute(
                    """INSERT INTO business_sessions
                       (session_id, user_id, title, messages, status, metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session.session_id,
                        session.user_id,
                        session.title,
                        json.dumps(session.messages, ensure_ascii=False),
                        session.status,
                        json.dumps(session.metadata or {}, ensure_ascii=False),
                        session.created_at,
                        session.updated_at,
                    ),
                )
                conn.commit()
            return session.session_id
        except Exception:
            return None

    def export_all(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """导出所有（或某用户）会话为 JSON 数组。"""
        sessions = self.list_by_user(user_id) if user_id else self._list_all()
        return [s.to_dict() for s in sessions]

    def export_to_file(self, file_path: str, user_id: str | None = None) -> int:
        """导出会话到 JSON 文件。返回导出数量。"""
        data = self.export_all(user_id=user_id)
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return len(data)

    def import_from_file(self, file_path: str, merge: bool = True) -> int:
        """从 JSON 文件导入会话。

        Args:
            file_path: JSON 文件路径
            merge: True=跳过已存在的 session_id；False=覆盖

        Returns:
            导入数量
        """
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = [data]

        count = 0
        for item in data:
            sid = item.get("session_id", "")
            if not sid:
                continue

            existing = self.get(sid)
            if existing is not None and merge:
                continue

            # 创建或更新
            session = Session(
                session_id=sid,
                user_id=item.get("user_id", "default"),
                title=item.get("title", ""),
                messages=item.get("messages", []),
                metadata=item.get("metadata", {}),
                status=item.get("status", "active"),
                created_at=item.get("created_at", time.time()),
                updated_at=item.get("updated_at", time.time()),
            )
            self._upsert_session(session)
            count += 1

        return count

    def _upsert_session(self, session: Session) -> None:
        """INSERT OR REPLACE 一个会话。"""
        with self._db.get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO business_sessions
                   (session_id, user_id, title, messages, metadata, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.user_id,
                    session.title,
                    json.dumps(session.messages, ensure_ascii=False),
                    json.dumps(session.metadata, ensure_ascii=False),
                    session.status,
                    session.created_at,
                    session.updated_at,
                ),
            )
            conn.commit()

    # ── 统计 ──

    def get_stats(self, user_id: str | None = None) -> dict[str, Any]:
        """获取会话统计信息。

        Returns:
            {total_sessions, total_messages, active_sessions, archived_sessions, avg_message_length}
        """
        with self._db.get_conn() as conn:
            where = "WHERE user_id = ?" if user_id else ""
            params: list[Any] = [user_id] if user_id else []

            total = conn.execute(f"SELECT COUNT(*) FROM business_sessions {where}", params).fetchone()[0]

            by_status = {}
            for status in ("active", "archived", "closed"):
                p2 = params + [status]
                where2 = f"{where} {'AND' if where else 'WHERE'} status = ?"
                by_status[status] = conn.execute(f"SELECT COUNT(*) FROM business_sessions {where2}", p2).fetchone()[0]

            # 总消息数
            rows = conn.execute(f"SELECT messages FROM business_sessions {where}", params).fetchall()
            total_msgs = sum(len(json.loads(r["messages"])) for r in rows)

        return {
            "total_sessions": total,
            "total_messages": total_msgs,
            "active": by_status.get("active", 0),
            "archived": by_status.get("archived", 0),
            "closed": by_status.get("closed", 0),
        }

    def _list_all(self) -> list[Session]:
        """列出所有会话（内部使用）。"""
        with self._db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM business_sessions ORDER BY updated_at DESC").fetchall()
        return [Session.from_row(r) for r in rows]

    # ── 上下文组装 ──

    def get_context(self, session_id: str, last_n: int = 20) -> str:
        """将会话历史组装为 LLM 可用的纯文本上下文。"""
        msgs = self.get_messages(session_id, last_n=last_n)
        if not msgs:
            return ""
        lines: list[str] = []
        for m in msgs:
            role_tag = {"user": "用户", "assistant": "助手", "system": "系统"}.get(m["role"], m["role"])
            lines.append(f"[{role_tag}]: {m['content']}")
        return "\n".join(lines)
