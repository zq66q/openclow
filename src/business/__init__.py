"""业务层 — 将 Agent 零件组装为开箱即用的产品。

本层不发明新能力，而是编排 Layers 1-5 的基础设施，
提供会话管理、预置 Agent、工作流引擎、提示词模板、场景脚手架、统一服务入口。
"""

from business.session import SessionManager, Session
from business.presets import PresetAgents
from business.workflows import Workflow, WorkflowState, FlowStep, StepType, StepResult, WorkflowResult, build_sequential_workflow, build_parallel_workflow
from business.prompts import PromptLibrary, PromptTemplate
from business.scenarios import ScenarioBuilder, ScenarioApp, AppMetrics
from business.service_facade import ServiceFacade, ServiceConfig, ServiceStatus, HealthReport

__all__ = [
    # 会话
    "SessionManager",
    "Session",
    # 预置 Agent
    "PresetAgents",
    # 工作流
    "Workflow",
    "WorkflowState",
    "FlowStep",
    "StepType",
    "StepResult",
    "WorkflowResult",
    "build_sequential_workflow",
    "build_parallel_workflow",
    # 提示词
    "PromptLibrary",
    "PromptTemplate",
    # 场景
    "ScenarioBuilder",
    "ScenarioApp",
    "AppMetrics",
    # 服务入口
    "ServiceFacade",
    "ServiceConfig",
    "ServiceStatus",
    "HealthReport",
]
