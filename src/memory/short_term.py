"""短期记忆 — 滑动窗口对话历史 + 自动摘要压缩。

策略：
1. 保留最近 N 轮对话（按 token 估算，非精确 count）
2. 超窗口不丢弃，自动调用 mini 模型生成摘要
3. 摘要把冗长对话压缩为关键信息（实体 + 意图 + 决策）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from core.logger import logger
from core.settings import settings

if TYPE_CHECKING:
    from core.llm_client import BaseLLMClient


@dataclass
class Message:
    """单条对话消息。"""

    role: str  # "user" | "assistant" | "system" | "summary"
    content: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    def estimated_tokens(self) -> int:
        """粗略估算 token 数（中文 ≈ 字数 × 1.5，英文 ≈ 字数 ÷ 4）。"""
        text = self.content
        chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other_chars = len(text) - chinese_chars
        return int(chinese_chars * 1.5 + other_chars * 0.25) + 4  # +4 消息格式开销


class ShortTermMemory:
    """滑动窗口短期记忆。

    用法：
        stm = ShortTermMemory(max_tokens=4000)
        stm.add("user", "帮我查一下今天的天气")
        stm.add("assistant", "今天北京晴，25°C")
        history = stm.get_history()  # → [Message, ...]
    """

    def __init__(
        self,
        max_tokens: int | None = None,
        user_id: str = "default",
    ) -> None:
        self.max_tokens = max_tokens or settings.memory.short_term_max_tokens
        self.user_id = user_id
        self._messages: list[Message] = []
        self._summaries: list[str] = []  # 被压缩出去的历史摘要栈

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def add(self, role: str, content: str) -> Message:
        """添加一条消息，超窗口自动触发摘要压缩。"""
        msg = Message(role=role, content=content)
        self._messages.append(msg)

        # 检查是否超出 token 限制
        while self._current_tokens() > self.max_tokens and len(self._messages) > 4:
            self._compress_once()

        return msg

    def add_user(self, content: str) -> Message:
        return self.add("user", content)

    def add_assistant(self, content: str) -> Message:
        return self.add("assistant", content)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_history(self, last_n: int | None = None) -> list[Message]:
        """获取最近 N 条消息（默认全量）。"""
        if last_n is None:
            return list(self._messages)
        return self._messages[-last_n:]

    def get_history_dicts(self, last_n: int | None = None) -> list[dict[str, str]]:
        """获取 OpenAI 格式消息列表。"""
        return [m.to_dict() for m in self.get_history(last_n)]

    def get_summaries(self) -> list[str]:
        """获取所有历史摘要（压缩出去的内容）。"""
        return list(self._summaries)

    def get_context(self) -> str:
        """组装完整上下文供 LLM 使用。"""
        parts: list[str] = []

        if self._summaries:
            parts.append("## 历史摘要\n" + "\n".join(f"- {s}" for s in self._summaries))

        if self._messages:
            parts.append("## 最近对话")
            for m in self._messages:
                parts.append(f"[{m.role}]: {m.content}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 摘要压缩
    # ------------------------------------------------------------------

    def summarize(
        self,
        llm_client: BaseLLMClient | None = None,
        max_keep: int = 4,
    ) -> str | None:
        """将超出部分的关键信息抽取为一段摘要。

        Args:
            llm_client: LLM 客户端，为 None 时用简单截断
            max_keep: 保留最近 N 条不被压缩

        Returns:
            生成的摘要文本，没东西可压则返回 None
        """
        if len(self._messages) <= max_keep:
            return None

        to_compress = self._messages[:-max_keep]
        recent = self._messages[-max_keep:]

        # 优先 LLM 摘要
        if llm_client is not None:
            try:
                dialog = "\n".join(
                    f"[{m.role}]: {m.content}" for m in to_compress
                )
                prompt = (
                    "请将以下对话历史压缩为一段简短摘要（限 200 字），"
                    "只保留关键信息：实体名称、用户偏好、重要结论、待办事项。\n\n"
                    f"{dialog}"
                )
                summary, _ = llm_client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=300,
                )
            except Exception:
                logger.warning("LLM 摘要失败，回退到截断")
                summary = self._simple_summary(to_compress)
        else:
            summary = self._simple_summary(to_compress)

        # 更新状态
        self._summaries.append(summary)
        self._messages = recent

        logger.info(
            "short_term summarized",
            extra={
                "user_id": self.user_id,
                "compressed_count": len(to_compress),
                "remaining_count": len(recent),
                "total_summaries": len(self._summaries),
            },
        )
        return summary

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """清空所有短期记忆。"""
        self._messages.clear()
        self._summaries.clear()

    def token_count(self) -> int:
        return self._current_tokens()

    def message_count(self) -> int:
        return len(self._messages)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（供长期记忆持久化）。"""
        return {
            "user_id": self.user_id,
            "messages": [{"role": m.role, "content": m.content, "timestamp": m.timestamp} for m in self._messages],
            "summaries": self._summaries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShortTermMemory:
        """从字典恢复。"""
        stm = cls(user_id=data.get("user_id", "default"))
        for item in data.get("messages", []):
            stm._messages.append(Message(
                role=item["role"],
                content=item["content"],
                timestamp=item.get("timestamp", time.time()),
            ))
        stm._summaries = data.get("summaries", [])
        return stm

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _current_tokens(self) -> int:
        return sum(m.estimated_tokens() for m in self._messages)

    def _compress_once(self) -> None:
        """压缩最早的一条消息（不调 LLM，纯字符截断）。"""
        if len(self._messages) <= 2:
            return
        oldest = self._messages.pop(0)
        self._summaries.append(f"[{oldest.role}]: {oldest.content[:100]}...")

    @staticmethod
    def _simple_summary(messages: list[Message]) -> str:
        """无 LLM 时的简单摘要：取每条消息的前 60 字。"""
        parts = [f"[{m.role}]: {m.content[:60]}..." for m in messages]
        return "；".join(parts)
