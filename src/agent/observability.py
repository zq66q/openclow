"""Agent 可观测性 — 指标采集、追踪、健康检查。

提供：
  - AgentMetrics: 运行指标（调用次数、成功率、延迟、token 用量）
  - TraceContext: 跨 Agent 调用链追踪
  - HealthCheck: 依赖健康状态
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from core.logger import logger

# 追踪上下文
_trace_span: ContextVar[str] = ContextVar("trace_span", default="")


def get_trace_span() -> str:
    return _trace_span.get()


def set_trace_span(span_id: str) -> str:
    _trace_span.set(span_id)
    return span_id


@dataclass
class AgentMetrics:
    """单个 Agent 的运行时指标。"""

    agent_name: str = ""
    total_calls: int = 0
    success_calls: int = 0
    error_calls: int = 0
    total_latency_ms: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tool_calls: int = 0
    last_call_at: float = 0.0
    last_error: str = ""

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.success_calls / self.total_calls

    @property
    def avg_latency_ms(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_latency_ms / self.total_calls

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "total_calls": self.total_calls,
            "success_calls": self.success_calls,
            "error_calls": self.error_calls,
            "success_rate": round(self.success_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tool_calls": self.total_tool_calls,
            "last_call_at": self.last_call_at,
            "last_error": self.last_error,
        }


@dataclass
class HealthStatus:
    """系统健康状态。"""

    healthy: bool = True
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)


class AgentObservability:
    """Agent 可观测性管理器（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, AgentMetrics] = {}
        self._trace_spans: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 指标采集
    # ------------------------------------------------------------------

    def record_call(
        self,
        agent_name: str,
        *,
        success: bool,
        latency_ms: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        tool_calls: int = 0,
        error: str = "",
    ) -> None:
        """记录一次 Agent 调用。"""
        with self._lock:
            if agent_name not in self._metrics:
                self._metrics[agent_name] = AgentMetrics(agent_name=agent_name)

            m = self._metrics[agent_name]
            m.total_calls += 1
            if success:
                m.success_calls += 1
            else:
                m.error_calls += 1
                m.last_error = error
            m.total_latency_ms += latency_ms
            m.total_prompt_tokens += prompt_tokens
            m.total_completion_tokens += completion_tokens
            m.total_tool_calls += tool_calls
            m.last_call_at = time.time()

    def get_metrics(self, agent_name: str | None = None) -> dict[str, Any]:
        """获取指标快照。"""
        with self._lock:
            if agent_name:
                m = self._metrics.get(agent_name)
                return m.to_dict() if m else {}
            return {name: m.to_dict() for name, m in self._metrics.items()}

    def get_global_metrics(self) -> dict[str, Any]:
        """获取全局汇总指标。"""
        with self._lock:
            total_calls = sum(m.total_calls for m in self._metrics.values())
            total_tokens = sum(m.total_prompt_tokens + m.total_completion_tokens for m in self._metrics.values())
            return {
                "total_agents": len(self._metrics),
                "total_calls": total_calls,
                "total_tokens": total_tokens,
                "agents": {n: m.to_dict() for n, m in self._metrics.items()},
            }

    def reset_metrics(self, agent_name: str | None = None) -> None:
        """重置指标。"""
        with self._lock:
            if agent_name:
                self._metrics.pop(agent_name, None)
            else:
                self._metrics.clear()

    # ------------------------------------------------------------------
    # 链路追踪
    # ------------------------------------------------------------------

    def start_trace(self, span_name: str, metadata: dict | None = None) -> str:
        """开始一个追踪 span。返回 span_id。"""
        import uuid

        span_id = uuid.uuid4().hex[:12]
        self._trace_spans[span_id] = {
            "span_name": span_name,
            "metadata": metadata or {},
            "start_time": time.time(),
            "end_time": 0,
            "status": "running",
        }
        set_trace_span(span_id)
        return span_id

    def end_trace(self, span_id: str, status: str = "ok", error: str = "") -> None:
        """结束追踪 span。"""
        span = self._trace_spans.get(span_id)
        if span:
            span["end_time"] = time.time()
            span["status"] = status
            span["error"] = error
            duration_ms = (span["end_time"] - span["start_time"]) * 1000
            logger.debug(f"Trace [{span_id}] {span['span_name']}: {status} ({duration_ms:.0f}ms)")

    def get_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取最近的追踪记录。"""
        traces = sorted(
            self._trace_spans.values(),
            key=lambda s: s["start_time"],
            reverse=True,
        )
        return traces[:limit]

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    def health_check(self) -> HealthStatus:
        """检查系统健康状态。"""
        status = HealthStatus()
        checks: dict[str, bool] = {}
        details: dict[str, str] = {}

        # 检查 Agent 指标
        with self._lock:
            for name, m in self._metrics.items():
                if m.error_calls > 0 and m.success_rate < 0.5:
                    checks[f"agent:{name}"] = False
                    details[f"agent:{name}"] = f"成功率过低: {m.success_rate:.0%}"
                    status.healthy = False
                else:
                    checks[f"agent:{name}"] = True

        if not checks:
            checks["agents"] = True
            details["agents"] = "无 Agent 运行记录"

        status.checks = checks
        status.details = details
        status.checked_at = time.time()
        return status


# 全局单例
_observability: AgentObservability | None = None
_obs_lock = threading.Lock()


def get_observability() -> AgentObservability:
    """获取全局 AgentObservability 单例。"""
    global _observability
    if _observability is None:
        with _obs_lock:
            if _observability is None:
                _observability = AgentObservability()
    return _observability
