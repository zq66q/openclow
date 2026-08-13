"""Agent 执行器 — 编排单个 Agent 的运行，处理重试、超时、异常恢复（v2 增强）。

增强（v2）:
  - 指数退避重试（避免打爆限流）
  - 步骤超时保护（防止死循环）
  - 可观测性集成（自动记录指标）
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agent.base import AgentResult, AgentState
from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent


class AgentExecutor:
    """Agent 执行器（纯静态方法，无内部状态）。"""

    # 指数退避参数
    BACKOFF_BASE = 1.0     # 基础等待秒数
    BACKOFF_MAX = 30.0     # 最大等待秒数
    BACKOFF_MULTIPLIER = 2.0

    @staticmethod
    def run(agent: BaseAgent, query: str, step_callback: callable | None = None, image_data: str | None = None) -> AgentResult:
        """执行一次 Agent 推理。"""
        logger.info(
            f"AgentExecutor.run [{agent.name}]",
            extra={"loop_type": agent.loop_type, "query": query[:100]},
        )

        start_time = time.perf_counter()
        result: AgentResult

        if agent.loop_type == "function":
            from agent.function_loop import FunctionCallingLoop
            result = FunctionCallingLoop.run(agent, query, image_data=image_data)
        elif agent.loop_type == "plan_execute":
            from agent.plan_execute_loop import PlanExecuteLoop
            result = PlanExecuteLoop.run(agent, query, step_callback=step_callback, image_data=image_data)
        else:
            from agent.react_loop import ReActLoop
            result = ReActLoop.run(agent, query, step_callback=step_callback, image_data=image_data)

        # 记录可观测性指标
        try:
            from agent.observability import get_observability
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            obs = get_observability()
            obs.record_call(
                agent_name=agent.name,
                success=result.success,
                latency_ms=elapsed_ms,
                prompt_tokens=result.token_usage.get("prompt", 0),
                completion_tokens=result.token_usage.get("completion", 0),
                tool_calls=result.tool_calls_count,
                error=result.error or "",
            )
        except Exception:
            pass

        return result

    @staticmethod
    def run_with_retry(
        agent: BaseAgent,
        query: str,
        max_retries: int = 3,
        *,
        backoff: bool = True,
    ) -> AgentResult:
        """带指数退避的重试执行。

        Args:
            agent: Agent 实例
            query: 用户输入
            max_retries: 最大重试次数
            backoff: 是否使用指数退避
        """
        last_result: AgentResult | None = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                wait = AgentExecutor._calc_backoff(attempt) if backoff else 0
                logger.warning(
                    f"Agent [{agent.name}] retry {attempt}/{max_retries} (wait {wait:.1f}s)"
                )
                if wait > 0:
                    time.sleep(wait)

            agent.state = AgentState.IDLE
            result = AgentExecutor.run(agent, query)

            if result.success:
                if attempt > 0:
                    logger.info(f"Agent [{agent.name}] succeeded on retry {attempt}")
                return result

            last_result = result
            agent.state = AgentState.IDLE

        logger.error(
            f"Agent [{agent.name}] all retries exhausted",
            extra={"max_retries": max_retries},
        )

        if last_result:
            last_result.error = f"(重试 {max_retries} 次后仍失败) {last_result.error}"
            return last_result

        return AgentResult(error="重试耗尽，无可用结果", loop_type=agent.loop_type)

    @staticmethod
    def run_safe(agent: BaseAgent, query: str) -> AgentResult:
        """捕获所有异常的执行（绝不抛异常）。"""
        try:
            return AgentExecutor.run(agent, query)
        except Exception as exc:
            logger.error(
                f"AgentExecutor.run_safe [{agent.name}] unhandled error",
                extra={"error": str(exc)},
            )
            agent.state = AgentState.ERROR
            return AgentResult(error=f"未预料的异常: {exc}", loop_type=agent.loop_type, total_elapsed_ms=0)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_backoff(attempt: int) -> float:
        """计算指数退避等待时间。"""
        wait = AgentExecutor.BACKOFF_BASE * (AgentExecutor.BACKOFF_MULTIPLIER ** (attempt - 1))
        return min(wait, AgentExecutor.BACKOFF_MAX)
