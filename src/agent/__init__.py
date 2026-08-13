"""Agent 代理层 — LLM 智能体的基类、推理循环、路由、融合、应答与成本管理。"""

from agent.base import (
    AgentCancelledError,
    AgentResult,
    AgentState,
    AgentStep,
    BaseAgent,
    CancellationToken,
    StreamChunk,
)
from agent.context_builder import ContextBuilder
from agent.cost_tracker import CostReport, CostTracker
from agent.executor import AgentExecutor
from agent.function_loop import FunctionCallingLoop
from agent.fusion import FusionResult, FusionStrategy, ResultFusion
from agent.human_review import (
    HumanReview,
    ReviewAction,
    ReviewRequest,
    ReviewResult,
)
from agent.observability import (
    AgentMetrics,
    AgentObservability,
    HealthStatus,
    get_observability,
)
from agent.react_loop import ReActLoop
from agent.responder import Response, ResponseFormat, ResponseGenerator
from agent.router import AgentRouter, AgentTeam, RouteDecision, RouteStrategy, SubTask
from agent.state import AgentContext, StepStatus, WorkflowStep
from agent.state_store import StateStore

__all__ = [
    # 底座
    "BaseAgent",
    "AgentResult",
    "AgentState",
    "AgentStep",
    "StreamChunk",
    "CancellationToken",
    "AgentCancelledError",
    # 引擎
    "ContextBuilder",
    "AgentExecutor",
    "ReActLoop",
    "FunctionCallingLoop",
    # 路由与编排
    "AgentRouter",
    "AgentTeam",
    "RouteDecision",
    "RouteStrategy",
    "SubTask",
    # 融合
    "ResultFusion",
    "FusionResult",
    "FusionStrategy",
    # 应答
    "ResponseGenerator",
    "Response",
    "ResponseFormat",
    # 人在回路
    "HumanReview",
    "ReviewRequest",
    "ReviewResult",
    "ReviewAction",
    # 状态
    "AgentContext",
    "WorkflowStep",
    "StepStatus",
    "StateStore",
    # 成本
    "CostTracker",
    "CostReport",
    # 可观测性
    "AgentMetrics",
    "HealthStatus",
    "AgentObservability",
    "get_observability",
]
