"""长期记忆 — SQLite 持久化 + ChromaDB 语义检索。

双存储模型：
- SQLite: 结构化键值对（用户偏好、事实、标签），快速精确查询
- ChromaDB: 语义向量，支持模糊语义召回（复用 rag.VectorStore）

SQLite 连接管理已抽到 memory.db.MemoryDB，本模块只负责 CRUD。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from core.logger import logger
from core.settings import settings
from memory.db import MemoryDB


@dataclass
class MemoryEntry:
    """长期记忆条目。"""

    memory_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = "default"
    memory_type: str = "fact"  # "fact" | "episode" | "preference" | "knowledge"
    key: str = ""
    value: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5  # 0~1，越高越重要
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_row(self) -> tuple:
        return (
            self.memory_id,
            self.user_id,
            self.memory_type,
            self.key,
            self.value,
            json.dumps(self.metadata, ensure_ascii=False),
            self.importance,
            self.created_at,
            self.updated_at,
        )

    @classmethod
    def from_row(cls, row: tuple) -> MemoryEntry:
        return cls(
            memory_id=row[0],
            user_id=row[1],
            memory_type=row[2],
            key=row[3],
            value=row[4],
            metadata=json.loads(row[5]) if row[5] else {},
            importance=row[6],
            created_at=row[7],
            updated_at=row[8],
        )


class SQLiteMemoryStore:
    """SQLite 记忆存储 — 精确查询。"""

    def __init__(
        self,
        db: MemoryDB | None = None,
        db_path: str | None = None,
    ) -> None:
        self._db = db or MemoryDB(db_path=db_path)
        self._lock = Lock()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def upsert(self, entry: MemoryEntry) -> str:
        """写入或更新一条记忆。"""
        entry.updated_at = time.time()
        with self._lock, self._db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO memories (memory_id, user_id, memory_type, key, value,
                                      metadata, importance, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    value       = excluded.value,
                    importance  = excluded.importance,
                    metadata    = excluded.metadata,
                    updated_at  = excluded.updated_at
                """,
                entry.to_row(),
            )
            conn.commit()
        logger.debug("memory upsert", extra={"memory_id": entry.memory_id, "key": entry.key})
        return entry.memory_id

    def get_by_id(self, memory_id: str) -> MemoryEntry | None:
        with self._lock, self._db.get_conn() as conn:
            row = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        return MemoryEntry.from_row(row) if row else None

    def get_by_key(self, user_id: str, key: str) -> MemoryEntry | None:
        with self._lock, self._db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND key = ? ORDER BY updated_at DESC LIMIT 1",
                (user_id, key),
            ).fetchone()
        return MemoryEntry.from_row(row) if row else None

    def query(
        self,
        user_id: str,
        memory_type: str | None = None,
        top_k: int | None = None,
        min_importance: float = 0.0,
    ) -> list[MemoryEntry]:
        """按条件查询记忆列表。"""
        top_k = top_k or settings.memory.long_term_top_k
        sql = "SELECT * FROM memories WHERE user_id = ?"
        params: list[Any] = [user_id]

        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)

        if min_importance > 0:
            sql += " AND importance >= ?"
            params.append(min_importance)

        sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        params.append(top_k)

        with self._lock, self._db.get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [MemoryEntry.from_row(r) for r in rows]

    def delete(self, memory_id: str) -> bool:
        with self._lock, self._db.get_conn() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
        return deleted

    def delete_by_key(self, user_id: str, key: str) -> int:
        with self._lock, self._db.get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE user_id = ? AND key = ?",
                (user_id, key),
            )
            conn.commit()
            return cursor.rowcount

    def count(self, user_id: str | None = None) -> int:
        with self._lock, self._db.get_conn() as conn:
            if user_id:
                row = conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0] if row else 0

    def get_user_profile(self, user_id: str) -> dict[str, Any]:
        """获取用户画像摘要。"""
        facts = self.query(user_id, memory_type="fact")
        preferences = self.query(user_id, memory_type="preference")
        return {
            "user_id": user_id,
            "facts": {f.key: f.value for f in facts},
            "preferences": {p.key: p.value for p in preferences},
            "total_memories": self.count(user_id),
        }


class SemanticMemoryStore:
    """语义记忆 — 基于 ChromaDB 的模糊召回。

    用于「用户喜欢什么」→「他的偏好是什么」这类语义相似问题，
    不要求精确 key 匹配。
    """

    def __init__(
        self,
        collection_name: str = "openclaw_memory",
    ) -> None:
        from rag.vector_store import VectorStore

        self._store = VectorStore(collection_name=collection_name)

    def add_memory(
        self,
        text: str,
        metadata: dict[str, Any],
        vector: list[float],
    ) -> str:
        """添加一条语义记忆。

        Args:
            text: 记忆文本
            metadata: 关联元数据
            vector: 文本的 embedding 向量

        Returns:
            写入的 id
        """
        ids = self._store.add(
            texts=[text],
            vectors=[vector],
            metadatas=[metadata],
        )
        return ids[0]

    def recall(
        self,
        query_vector: list[float],
        user_id: str | None = None,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """语义召回 — 搜相似记忆。

        Args:
            query_vector: 查询文本的 embedding 向量
            user_id: 限定用户
            top_k: 返回条数

        Returns:
            [{"id", "text", "metadata", "score"}, ...]
        """
        where = {"user_id": user_id} if user_id else None
        results = self._store.search(query_vector, top_k=top_k, where=where)
        return results

    def delete(self, memory_ids: list[str]) -> None:
        self._store.delete(memory_ids)

    def count(self) -> int:
        return self._store.count()
