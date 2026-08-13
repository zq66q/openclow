"""记忆管理器 — 三层记忆统一入口。

职责：
- 短期记忆（当前会话的对话历史，滑动窗口 + 摘要压缩）
- 长期记忆（跨会话持久化，SQLite 结构化 + ChromaDB 语义）
- 自动路由：add_message → 短期；save_fact → 长期；recall → 短期 + 长期

这是 Agent 调用记忆系统的唯一入口，一个 API 搞定所有记忆操作。
"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from core.logger import logger
from core.settings import settings
from memory.db import MemoryDB
from memory.short_term import ShortTermMemory
from memory.long_term import (
    MemoryEntry,
    SQLiteMemoryStore,
    SemanticMemoryStore,
)

if TYPE_CHECKING:
    from core.llm_client import BaseLLMClient
    from memory.extractor import MemoryExtractor
    from memory.compressor import MemoryCompressor


class MemoryManager:
    """三层记忆统一管理器。

    用法:
        mm = MemoryManager(user_id="user_001")
        mm.add_message("user", "帮我写一个排序函数")
        mm.add_message("assistant", "好的，这是一个快速排序...")

        # 存入长期记忆
        mm.save_fact("likes_python", True, importance=0.8)

        # 查询
        facts = mm.recall_facts(["likes_python"])
        profile = mm.get_user_profile()
        context = mm.get_context()  # 组装好的 LLM prompt 上下文
    """

    def __init__(
        self,
        user_id: str = "default",
        db_path: str | None = None,
    ) -> None:
        self.user_id = user_id
        # 共享同一个 MemoryDB 实例（SQLite 连接池）
        self._db = MemoryDB(db_path=db_path)
        self.short_term = ShortTermMemory(user_id=user_id)
        self.sql_store = SQLiteMemoryStore(db=self._db)
        self.semantic_store = SemanticMemoryStore()
        self._last_summary_at = time.time()
        # 高级功能（按需开启）
        self._extractor: MemoryExtractor | None = None
        self._compressor: MemoryCompressor | None = None

    # ------------------------------------------------------------------
    # 短期记忆 — 对话流
    # ------------------------------------------------------------------

    def add_message(self, role: str, content: str) -> None:
        """添加一条对话消息到短期记忆。

        如果超窗口会自动触发摘要压缩。
        """
        self.short_term.add(role, content)
        logger.debug("memory add_message", extra={"user_id": self.user_id, "role": role})

    def add_user_message(self, content: str) -> None:
        self.add_message("user", content)

    def add_assistant_message(self, content: str) -> None:
        self.add_message("assistant", content)

    def get_recent_history(self, last_n: int | None = None) -> list[dict[str, str]]:
        """获取最近的 OpenAI 格式对话历史。"""
        return self.short_term.get_history_dicts(last_n)

    def get_current_summary(self) -> str:
        """获取历史摘要（如有）。"""
        summaries = self.short_term.get_summaries()
        return "\n".join(f"- {s}" for s in summaries) if summaries else ""

    # ------------------------------------------------------------------
    # 摘要 — 周期性压缩
    # ------------------------------------------------------------------

    def try_summarize(
        self,
        llm_client: BaseLLMClient | None = None,
        force: bool = False,
    ) -> str | None:
        """按配置的 summary_interval 周期触发摘要。

        Returns:
            生成的摘要文本，未触发则 None
        """
        interval = settings.memory.short_term_max_tokens
        message_count = self.short_term.message_count()

        if not force and message_count < 10:
            return None

        if not force and (time.time() - self._last_summary_at) < 60:
            return None

        summary = self.short_term.summarize(llm_client=llm_client)

        if summary:
            self._last_summary_at = time.time()
            # 将摘要存入长期记忆
            self.save_fact(
                key=f"summary_{int(time.time())}",
                value=summary,
                memory_type="episode",
                importance=0.6,
            )

        return summary

    # ------------------------------------------------------------------
    # 长期记忆 — 结构化
    # ------------------------------------------------------------------

    def save_fact(
        self,
        key: str,
        value: Any,
        memory_type: str = "fact",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """保存一条结构化记忆（键值对 + 重要性 + 元数据）。

        Args:
            key: 记忆键（如 "favorite_language"）
            value: 记忆值（如 "Python"），自动转为字符串
            memory_type: 类型 — "fact" | "preference" | "knowledge"
            importance: 重要度 0~1
            metadata: 附加元数据

        Returns:
            memory_id
        """
        value_str = str(value) if not isinstance(value, str) else value
        existing = self.sql_store.get_by_key(self.user_id, key)

        if existing:
            existing.value = value_str
            existing.importance = max(existing.importance, importance)
            existing.metadata.update(metadata or {})
            return self.sql_store.upsert(existing)

        entry = MemoryEntry(
            user_id=self.user_id,
            memory_type=memory_type,
            key=key,
            value=value_str,
            importance=importance,
            metadata=metadata or {},
        )
        memory_id = self.sql_store.upsert(entry)
        logger.info("memory save_fact", extra={"user_id": self.user_id, "key": key, "importance": importance})
        return memory_id

    def save_memory(
        self,
        text: str,
        vector: list[float] | None = None,
        memory_type: str = "fact",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """同时写入 SQLite 结构化 + ChromaDB 语义记忆。

        Returns:
            {"sql_id": str, "semantic_id": str}
        """
        meta = dict(metadata or {})
        meta["user_id"] = self.user_id
        meta["memory_type"] = memory_type

        # SQLite 结构化
        entry = MemoryEntry(
            user_id=self.user_id,
            memory_type=memory_type,
            key=f"mem_{int(time.time())}",
            value=text,
            importance=importance,
            metadata=meta,
        )
        sql_id = self.sql_store.upsert(entry)

        # ChromaDB 语义
        sem_id = ""
        if vector is not None:
            sem_id = self.semantic_store.add_memory(
                text=text,
                metadata=meta,
                vector=vector,
            )

        return {"sql_id": sql_id, "semantic_id": sem_id}

    # ------------------------------------------------------------------
    # 长期记忆 — 查询
    # ------------------------------------------------------------------

    def recall_facts(self, keys: list[str] | None = None) -> dict[str, str]:
        """精确查询结构化记忆。

        Args:
            keys: 要查的 key 列表，None 则返回全部

        Returns:
            {key: value, ...}
        """
        if keys:
            result: dict[str, str] = {}
            for k in keys:
                entry = self.sql_store.get_by_key(self.user_id, k)
                if entry:
                    result[k] = entry.value
            return result

        entries = self.sql_store.query(
            self.user_id,
            top_k=settings.memory.long_term_top_k * 2,
        )
        return {e.key: e.value for e in entries}

    def recall_by_type(
        self,
        memory_type: str,
        top_k: int | None = None,
    ) -> list[MemoryEntry]:
        """按类型查询记忆列表。"""
        return self.sql_store.query(
            self.user_id,
            memory_type=memory_type,
            top_k=top_k,
        )

    def recall_semantic(
        self,
        query_vector: list[float],
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """语义召回 — 相似记忆。"""
        return self.semantic_store.recall(
            query_vector=query_vector,
            user_id=self.user_id,
            top_k=top_k,
        )

    # ------------------------------------------------------------------
    # 用户画像
    # ------------------------------------------------------------------

    def get_user_profile(self) -> dict[str, Any]:
        """获取当前用户的画像（偏好 + 事实 + 统计）。"""
        return self.sql_store.get_user_profile(self.user_id)

    def get_preferences(self) -> dict[str, str]:
        """获取用户偏好键值对。"""
        entries = self.sql_store.query(self.user_id, memory_type="preference")
        return {e.key: e.value for e in entries}

    # ------------------------------------------------------------------
    # 上下文组装
    # ------------------------------------------------------------------

    def get_context(self, max_facts: int = 5) -> str:
        """组装完整的 Agent 上下文（短期对话 + 长期记忆 + 用户画像）。

        Returns:
            可直接注入 LLM system prompt 的上下文文本
        """
        parts: list[str] = []

        # 1. 用户画像
        profile = self.get_user_profile()
        if profile["preferences"] or profile["facts"]:
            parts.append("## 用户画像")
            for k, v in profile["preferences"].items():
                parts.append(f"- 偏好: {k} = {v}")
            for k, v in profile["facts"].items():
                parts.append(f"- 信息: {k} = {v}")

        # 2. 历史摘要
        summaries = self.short_term.get_summaries()
        if summaries:
            parts.append("## 历史摘要")
            for s in summaries:
                parts.append(f"- {s}")

        # 3. 近期对话
        recent = self.short_term.get_history_dicts(last_n=10)
        if recent:
            parts.append("## 最近对话")
            for m in recent:
                parts.append(f"[{m['role']}]: {m['content']}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 自动学习 — extractor + compressor
    # ------------------------------------------------------------------

    def enable_auto_learning(self, llm_client: BaseLLMClient) -> MemoryExtractor:
        """一键开启自动学习：创建 extractor 和 compressor。

        之后每次 add_message 都可以通过 compressor 自动压缩 + 抽取事实。
        后台循环需自行 await compressor.run()。

        Returns:
            MemoryExtractor 实例，也可通过 mm._extractor 访问
        """
        from memory.extractor import MemoryExtractor
        from memory.compressor import MemoryCompressor

        self._extractor = MemoryExtractor(llm_client, self)
        self._compressor = MemoryCompressor(llm_client, self, self._extractor)
        logger.info(
            "auto_learning enabled",
            extra={"user_id": self.user_id},
        )
        return self._extractor

    def extract_and_save(self, dialog_text: str) -> int:
        """从对话文本中抽取事实并存入长期记忆（便捷方法）。

        必须先调用 enable_auto_learning() 初始化 extractor。
        """
        if self._extractor is None:
            logger.warning("extract_and_save called before enable_auto_learning")
            return 0
        return self._extractor.extract_and_save(dialog_text)

    @property
    def compressor(self) -> MemoryCompressor | None:
        """获取后台压缩器实例（enable_auto_learning 后可用）。"""
        return self._compressor

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    def forget(self, key: str | None = None) -> int:
        """删除记忆。key 为 None 则清空当前用户所有记忆。"""
        if key:
            return self.sql_store.delete_by_key(self.user_id, key)
        entries = self.sql_store.query(self.user_id, top_k=10000)
        deleted = 0
        for e in entries:
            if self.sql_store.delete(e.memory_id):
                deleted += 1
        return deleted

    def clear(self) -> None:
        """清空当前会话的短期记忆（不删长期）。"""
        self.short_term.clear()

    def stats(self) -> dict[str, Any]:
        """记忆系统统计。"""
        return {
            "user_id": self.user_id,
            "short_term_messages": self.short_term.message_count(),
            "short_term_tokens": self.short_term.token_count(),
            "long_term_count": self.sql_store.count(self.user_id),
            "summaries": len(self.short_term.get_summaries()),
        }
