"""场景脚手架 — 一行代码搭建完整业务场景（生产增强版 v2）。

将 PresetAgents + SessionManager + PromptLibrary 组合为开箱即用的应用。

增强（v2）:
  - 错误边界 — chat() 永不抛异常
  - 健康检查
  - 指标收集（聊天数、延迟、错误率）
  - 速率限制支持

用法:
    # 知识库客服
    app = ScenarioBuilder.rag_customer_service(docs_dir="./knowledge")

    # 数据分析助手
    app = ScenarioBuilder.data_analyst()

    # 开始对话
    app.chat("最近三个月销售额怎么样？")

    # 健康检查
    status = app.health_check()
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

from business.session import Session, SessionManager
from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent
    from business.session import Session, SessionManager
    from memory.memory_manager import MemoryManager
    from rag.pipeline import RAGPipeline


@dataclass
class AppMetrics:
    """应用级指标收集。"""

    total_chats: int = 0
    successful_chats: int = 0
    failed_chats: int = 0
    total_latency_ms: float = 0
    last_error: str | None = None
    last_chat_at: float = 0

    @property
    def avg_latency_ms(self) -> float:
        if self.total_chats == 0:
            return 0
        return self.total_latency_ms / self.total_chats

    @property
    def error_rate(self) -> float:
        if self.total_chats == 0:
            return 0
        return self.failed_chats / self.total_chats

    @property
    def is_healthy(self) -> bool:
        """连续失败不超过 3 次视为健康。"""
        # 简化判断：最近 10 次中失败不超过 3 次
        recent_window = min(self.total_chats, 10)
        if recent_window == 0:
            return True
        return (recent_window - self.failed_chats) >= max(recent_window - 3, 0)


class ScenarioApp:
    """由 ScenarioBuilder 创建的业务应用实例。

    提供统一的 chat() / health_check() 接口。
    内置错误边界 — chat() 永不抛异常，总是返回字符串。
    """

    def __init__(
        self,
        name: str,
        agent: BaseAgent,
        session_manager: SessionManager | None = None,
        memory_manager: MemoryManager | None = None,
        auto_save: bool = True,
        rate_limit_per_minute: int = 0,
        cost_tracker: Any = None,
    ) -> None:
        self.name = name
        self.agent = agent
        self.session_manager = session_manager or SessionManager()
        self.memory = memory_manager
        self._auto_save = auto_save
        self._current_session: Session | None = None
        self._metrics = AppMetrics()
        self._metrics_lock = Lock()
        self._rate_limit_per_minute = rate_limit_per_minute
        self.cost_tracker = cost_tracker
        self._last_request_times: list[float] = []

    # ── 对话 ──

    def chat(self, query: str, session_id: str | None = None, image_data: str | None = None) -> str:
        """执行一次对话并返回回答（内置错误边界，永不抛异常）。

        Args:
            query: 用户输入
            session_id: 指定会话（None 则自动创建或复用最近会话）
            image_data: base64 编码的图片数据（多模态支持）

        Returns:
            Agent 回答文本（错误时返回错误信息）
        """
        result = self.chat_with_details(query, session_id=session_id, image_data=image_data)
        return result["answer"]

    def chat_with_details(
        self, query: str, session_id: str | None = None, image_data: str | None = None
    ) -> dict[str, Any]:
        """执行一次对话并返回完整结果（含工具调用链、步骤详情）。

        Args:
            query: 用户输入
            session_id: 指定会话
            image_data: base64 编码的图片数据（多模态支持）

        Returns:
            {
                "answer": str,
                "success": bool,
                "steps": list[dict],
                "tool_calls_count": int,
                "elapsed_ms": float,
                "token_usage": dict,
                "error": str | None,
            }
        """
        t0 = time.perf_counter()

        try:
            # 速率限制
            if self._rate_limit_per_minute > 0:
                self._check_rate_limit()

            # 会话管理
            if session_id:
                self._current_session = self.session_manager.get(session_id)
            if self._current_session is None:
                self._current_session = self.session_manager.create(
                    user_id="default",
                    title=query[:30],
                )

            # 注入记忆上下文到 Agent 的 system prompt（如果有 memory）
            if self.memory and self.agent.memory is None:
                self.agent._memory = self.memory

            # 记录用户消息
            if self._auto_save:
                self.session_manager.append_message(self._current_session.session_id, "user", query)

            # 执行 Agent（传入图片数据）
            result = self.agent.run(query, image_data=image_data)
            elapsed = (time.perf_counter() - t0) * 1000

            answer = result.answer if result.success else f"[错误] {result.error}"
            logger.info(
                f"ScenarioApp [{self.name}] chat done",
                extra={"elapsed_ms": elapsed, "success": result.success},
            )

            # 记录助手回复
            if self._auto_save:
                self.session_manager.append_message(self._current_session.session_id, "assistant", answer)

            # 记忆注入
            if self.memory:
                self.memory.add_message("user", query)
                self.memory.add_message("assistant", answer)

            # 记录 cost_tracker
            if self.cost_tracker and result.token_usage:
                self.cost_tracker.record_tokens(
                    agent_name=self.agent.name,
                    prompt_tokens=result.token_usage.get("prompt", 0),
                    completion_tokens=result.token_usage.get("completion", 0),
                )
            if self.cost_tracker and result.tool_calls_count:
                for _ in range(result.tool_calls_count):
                    self.cost_tracker.record_tool_call(agent_name=self.agent.name)

            # 更新指标
            with self._metrics_lock:
                self._metrics.total_chats += 1
                self._metrics.successful_chats += 1
                self._metrics.total_latency_ms += elapsed
                self._metrics.last_chat_at = time.time()
                self._metrics.last_error = None

            return {
                "answer": answer,
                "success": result.success,
                "steps": [
                    {
                        "step_num": s.step_num,
                        "thought": s.thought,
                        "action": s.action,
                        "action_input": s.action_input,
                        "observation": s.observation,
                        "elapsed_ms": s.elapsed_ms,
                    }
                    for s in result.steps
                ],
                "tool_calls_count": result.tool_calls_count,
                "elapsed_ms": round(elapsed, 2),
                "token_usage": dict(result.token_usage),
                "error": result.error,
            }

        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            error_msg = f"[系统错误] {type(exc).__name__}: {exc}"
            logger.error(f"ScenarioApp [{self.name}] chat error: {exc}", exc_info=True)

            with self._metrics_lock:
                self._metrics.total_chats += 1
                self._metrics.failed_chats += 1
                self._metrics.total_latency_ms += elapsed
                self._metrics.last_error = str(exc)
                self._metrics.last_chat_at = time.time()

            return {
                "answer": error_msg,
                "success": False,
                "steps": [],
                "tool_calls_count": 0,
                "elapsed_ms": round(elapsed, 2),
                "token_usage": {},
                "error": str(exc),
            }

    def _check_rate_limit(self) -> None:
        """检查速率限制，超限时短暂等待。"""
        now = time.time()
        window = 60.0
        # 清理过期记录
        self._last_request_times = [t for t in self._last_request_times if now - t < window]
        if len(self._last_request_times) >= self._rate_limit_per_minute:
            oldest = self._last_request_times[0]
            wait = window - (now - oldest) + 0.1
            if wait > 0:
                logger.warning(f"ScenarioApp [{self.name}] rate limit, waiting {wait:.1f}s")
                time.sleep(wait)
        self._last_request_times.append(now)

    def new_session(self, title: str = "", user_id: str = "default") -> Session:
        """创建新会话。"""
        self._current_session = self.session_manager.create(
            user_id=user_id,
            title=title,
        )
        return self._current_session

    def get_history(self, session_id: str | None = None, last_n: int = 20) -> list[dict[str, str]]:
        """获取会话历史消息。"""
        sid = session_id or (self._current_session.session_id if self._current_session else None)
        if sid is None:
            return []
        return self.session_manager.get_messages(sid, last_n=last_n)

    def get_context(self, session_id: str | None = None) -> str:
        """获取会话上下文（组装好的纯文本）。"""
        sid = session_id or (self._current_session.session_id if self._current_session else None)
        if sid is None:
            return ""
        return self.session_manager.get_context(sid)

    # ── 健康检查 ──

    def health_check(self) -> dict[str, Any]:
        """返回应用健康状态。"""
        with self._metrics_lock:
            metrics = self._metrics
        return {
            "name": self.name,
            "healthy": metrics.is_healthy,
            "agent": self.agent.name,
            "agent_state": self.agent.state.value if hasattr(self.agent.state, "value") else str(self.agent.state),
            "total_chats": metrics.total_chats,
            "success_rate": round(1 - metrics.error_rate, 4) if metrics.total_chats > 0 else 1.0,
            "avg_latency_ms": round(metrics.avg_latency_ms, 2),
            "last_error": metrics.last_error,
            "auto_save": self._auto_save,
        }

    @property
    def metrics(self) -> AppMetrics:
        """获取当前指标快照。"""
        with self._metrics_lock:
            return AppMetrics(
                total_chats=self._metrics.total_chats,
                successful_chats=self._metrics.successful_chats,
                failed_chats=self._metrics.failed_chats,
                total_latency_ms=self._metrics.total_latency_ms,
                last_error=self._metrics.last_error,
                last_chat_at=self._metrics.last_chat_at,
            )


# ── 场景构建器 ──


class ScenarioBuilder:
    """一行代码搭建完整业务场景。

    所有工厂方法返回 ScenarioApp 实例，提供统一的 chat() 接口。

    用法:
        # 知识库客服
        app = ScenarioBuilder.rag_customer_service(docs_dir="./knowledge")
        print(app.chat("退货流程是什么？"))

        # 数据分析助手
        app = ScenarioBuilder.data_analyst()
        print(app.chat("上个月销售额增长率？"))
    """

    @staticmethod
    def rag_customer_service(
        docs_dir: str | None = None,
        rag_pipeline: RAGPipeline | None = None,
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        cost_tracker: Any = None,
    ) -> ScenarioApp:
        """搭建知识库客服场景。

        Args:
            docs_dir: 知识库文档目录，自动 ingest
            rag_pipeline: 已有 RAG 流水线（优先级高于 docs_dir）
            llm_client: LLM 客户端
            memory_manager: 记忆管理器
        """
        from business.presets import PresetAgents

        # RAG 准备
        pipeline = rag_pipeline
        if pipeline is None and docs_dir:
            from core.embedding_client import EmbeddingClient
            from rag.pipeline import RAGPipeline

            embed_client = EmbeddingClient()
            pipeline = RAGPipeline(
                collection_name="customer_service_kb",
                embed_client=embed_client,
            )
            pipeline.ingest_directory(docs_dir)
            logger.info(f"ScenarioBuilder: RAG ingested from {docs_dir}")

        agent = PresetAgents.rag_qa(
            rag_pipeline=pipeline,
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

        return ScenarioApp(
            name="知识库客服",
            agent=agent,
            memory_manager=memory_manager,
            cost_tracker=cost_tracker,
        )

    @staticmethod
    def data_analyst(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        cost_tracker: Any = None,
    ) -> ScenarioApp:
        """搭建数据分析助手场景。"""
        from business.presets import PresetAgents

        agent = PresetAgents.data_analysis(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

        return ScenarioApp(
            name="数据分析助手",
            agent=agent,
            memory_manager=memory_manager,
            cost_tracker=cost_tracker,
        )

    @staticmethod
    def code_reviewer(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        cost_tracker: Any = None,
    ) -> ScenarioApp:
        """搭建代码审查场景。"""
        from business.presets import PresetAgents

        agent = PresetAgents.code_review(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

        return ScenarioApp(
            name="代码审查",
            agent=agent,
            memory_manager=memory_manager,
            cost_tracker=cost_tracker,
        )

    @staticmethod
    def general_assistant(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        cost_tracker: Any = None,
    ) -> ScenarioApp:
        """搭建通用助手场景。"""
        from business.presets import PresetAgents

        agent = PresetAgents.general(
            llm_client=llm_client,
            memory_manager=memory_manager,
        )

        return ScenarioApp(
            name="通用助手",
            agent=agent,
            memory_manager=memory_manager,
            cost_tracker=cost_tracker,
        )

    @staticmethod
    def plan_executor(
        llm_client: Any = None,
        memory_manager: MemoryManager | None = None,
        rag_pipeline: RAGPipeline | None = None,
        cost_tracker: Any = None,
    ) -> ScenarioApp:
        """搭建 Plan-and-Execute 规划执行场景。"""
        from business.presets import PresetAgents

        agent = PresetAgents.plan_execute(
            llm_client=llm_client,
            memory_manager=memory_manager,
            rag_pipeline=rag_pipeline,
        )

        return ScenarioApp(
            name="Plan-and-Execute 规划执行",
            agent=agent,
            memory_manager=memory_manager,
            cost_tracker=cost_tracker,
        )
