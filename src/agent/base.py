# Agent 抽象基类

"""Agent 基类 — 定义智能体的生命周期、属性与核心接口（增强版 v2）。

增强（v2）:
  - async run / arun: 异步执行接口
  - streaming: 流式输出支持
  - cancellation: 取消令牌
  - 可观测性钩子
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.logger import logger

if TYPE_CHECKING:
    from memory.memory_manager import MemoryManager
    from rag.pipeline import RAGPipeline


class AgentState(str, Enum):
    """Agent 运行时状态。"""

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class AgentStep:
    """单个推理步骤的记录。"""

    step_num: int
    thought: str = ""
    action: str = ""
    action_input: dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    elapsed_ms: float = 0


@dataclass
class AgentResult:
    """Agent 执行结果。"""

    answer: str = ""
    steps: list[AgentStep] = field(default_factory=list)
    tool_calls_count: int = 0
    loop_type: str = ""
    error: str | None = None
    total_elapsed_ms: float = 0
    token_usage: dict[str, int] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass
class StreamChunk:
    """流式输出片段。"""

    content: str = ""
    is_final: bool = False
    event_type: str = ""  # "token" | "tool_call" | "thought" | "final"
    metadata: dict[str, Any] = field(default_factory=dict)


# 取消令牌
class CancellationToken:
    """线程安全的取消令牌。"""

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self) -> None:
        """如果已取消则抛出异常。"""
        if self.is_cancelled:
            raise AgentCancelledError("Agent 执行已被取消")


class AgentCancelledError(Exception):
    """Agent 被取消的异常。"""

    pass


class BaseAgent:
    """Agent 抽象基类。

    类属性（子类覆盖）:
        name          — Agent 唯一标识
        system_prompt — 系统提示词模板
        tools         — 绑定的工具名列表
        max_steps     — 最大推理步数
        loop_type     — 推理循环类型: "react" | "function"
        temperature   — LLM 温度
    """

    name: str = ""
    system_prompt: str = "You are a helpful assistant."
    tools: list[str] = []
    max_steps: int = 10
    loop_type: str = "react"
    temperature: float = 0.7

    def __init__(
        self,
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        rag_pipeline: RAGPipeline | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._memory = memory_manager
        self._rag = rag_pipeline
        self._state = AgentState.IDLE
        self._cancel_token: CancellationToken | None = None
        # 视觉模型（多模态图片输入）— 由 ServiceFacade 注入
        self.vision_model: str | None = None
        # 视觉模型专用 client（独立 endpoint，避免影响主对话）
        self.vision_llm_client: Any = None

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    @property
    def state(self) -> AgentState:
        return self._state

    @state.setter
    def state(self, val: AgentState) -> None:
        if val != self._state:
            logger.info(f"Agent [{self.name}] state: {self._state.value} -> {val.value}")
        self._state = val

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_token is not None and self._cancel_token.is_cancelled

    def cancel(self) -> None:
        """取消当前执行。"""
        logger.info(f"Agent [{self.name}] cancellation requested")
        self._cancel_token = CancellationToken()
        self._cancel_token.cancel()
        self._state = AgentState.CANCELLED

    # ------------------------------------------------------------------
    # 懒加载依赖
    # ------------------------------------------------------------------

    @property
    def llm_client(self):
        if self._llm_client is None:
            from core.llm_client import get_llm_client

            self._llm_client = get_llm_client()
        return self._llm_client

    @property
    def memory(self) -> MemoryManager | None:
        return self._memory

    @memory.setter
    def memory(self, val: MemoryManager | None) -> None:
        self._memory = val

    @property
    def rag(self) -> RAGPipeline | None:
        return self._rag

    @rag.setter
    def rag(self, val: RAGPipeline | None) -> None:
        self._rag = val

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def run(
        self,
        query: str,
        *,
        cancel_token: CancellationToken | None = None,
        step_callback: callable | None = None,
        image_data: str | None = None,
    ) -> AgentResult:
        """同步执行入口。"""
        self._cancel_token = cancel_token
        from agent.executor import AgentExecutor

        return AgentExecutor.run(self, query, step_callback=step_callback, image_data=image_data)

    async def arun(self, query: str, *, cancel_token: CancellationToken | None = None) -> AgentResult:
        """异步执行入口（在线程池中运行同步 run，避免阻塞事件循环）。"""
        self._cancel_token = cancel_token
        loop = asyncio.get_running_loop()

        def _sync_run() -> AgentResult:
            return self.run(query, cancel_token=cancel_token)

        return await loop.run_in_executor(None, _sync_run)

    def run_stream(self, query: str) -> AsyncIterator[StreamChunk]:
        """流式执行入口（生成器）。

        Usage:
            async for chunk in agent.run_stream("问题"):
                if chunk.is_final:
                    print(chunk.content)
        """
        return self._stream_generator(query)

    async def _stream_generator(self, query: str) -> AsyncIterator[StreamChunk]:
        """流式生成器实现。"""
        yield StreamChunk(content="", event_type="start", metadata={"agent": self.name})

        try:
            result = self.run(query)
            if result.success:
                yield StreamChunk(
                    content=result.answer,
                    is_final=True,
                    event_type="final",
                    metadata={"token_usage": result.token_usage, "elapsed_ms": result.total_elapsed_ms},
                )
            else:
                yield StreamChunk(content=f"错误: {result.error}", is_final=True, event_type="error")
        except AgentCancelledError:
            yield StreamChunk(content="（已取消）", is_final=True, event_type="cancelled")
        except Exception as exc:
            yield StreamChunk(content=f"异常: {exc}", is_final=True, event_type="error")

    # ------------------------------------------------------------------
    # 工具描述
    # ------------------------------------------------------------------

    def get_tool_description(self) -> str:
        """生成人类可读的工具列表描述。"""
        if not self.tools:
            return "（无可用工具）"

        from mcp_tools.registry import get_registry

        registry = get_registry()
        lines: list[str] = []
        for tname in self.tools:
            meta = registry.get_tool_meta(tname)
            if meta:
                lines.append(f"- {meta['name']}: {meta['description']}")
            else:
                lines.append(f"- {tname}")
        return "\n".join(lines)
