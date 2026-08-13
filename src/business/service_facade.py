"""统一服务入口 — 生产级一键启动。

将 Layers 1-5 所有基础设施装配为一个可部署的服务，
提供配置加载、依赖注入、健康检查、优雅启停、错误边界。

用法:
    from business.service_facade import ServiceFacade

    # 方式 1: 配置文件
    svc = ServiceFacade.from_config("config.yaml")

    # 方式 2: 编程式
    svc = ServiceFacade(
        llm_config={"api_key": "sk-...", "base_url": "...", "model": "gpt-4o-mini"},
        memory_db_path="./data/memory.db",
        rag_persist_dir="./data/rag",
    )

    with svc:
        # 创建场景应用
        app = svc.create_scenario("general_assistant")
        answer = app.chat("你好")

        # 或运行工作流
        result = svc.run_workflow("data_analysis", "分析最近销售趋势")

        # 健康检查
        status = svc.health_check()
"""

from __future__ import annotations

import json
import os
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from core.logger import logger


# ── 数据类型 ──


class ServiceStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class ServiceConfig:
    """服务全局配置。"""

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2048

    # 视觉模型（多模态图片输入专用，可与主模型使用不同 endpoint）
    llm_vision_model: str = "gpt-4o"
    llm_vision_base_url: str = ""
    llm_vision_api_key: str = ""

    # 嵌入
    embed_api_key: str = ""
    embed_base_url: str = ""
    embed_model: str = "text-embedding-3-small"

    # 存储
    memory_db_path: str = "./data/memory.db"
    rag_persist_dir: str = "./data/rag"
    state_db_path: str = "./data/state.db"

    # 业务
    default_user_id: str = "default"
    max_session_history: int = 100
    auto_save: bool = True

    # 超时 & 重试
    agent_step_timeout: int = 60
    workflow_timeout: int = 600
    max_retries: int = 2

    # 预算
    budget_usd: float = 10.0

    @classmethod
    def from_file(cls, path: str) -> ServiceConfig:
        """从 JSON/YAML 配置文件加载。"""
        p = Path(path)
        if not p.exists():
            logger.warning(f"Config file not found: {path}, using defaults")
            return cls()

        content = p.read_text(encoding="utf-8")
        if p.suffix in (".yaml", ".yml"):
            try:
                import yaml
                data = yaml.safe_load(content) or {}
            except ImportError:
                data = json.loads(content)
        else:
            data = json.loads(content)

        # 展平嵌套配置
        flat: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                for sub_key, sub_val in value.items():
                    flat[f"{key}_{sub_key}"] = sub_val
            else:
                flat[key] = value

        return cls(**{k: v for k, v in flat.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_env(cls) -> ServiceConfig:
        """从环境变量加载（兼容 OPENCLAW_ 前缀和标准名）。"""
        mapping = {
            "OPENCLAW_LLM_API_KEY": "llm_api_key",
            "OPENCLAW_LLM_BASE_URL": "llm_base_url",
            "OPENCLAW_LLM_MODEL": "llm_model",
            "OPENCLAW_LLM_VISION_MODEL": "llm_vision_model",
            "OPENCLAW_LLM_VISION_BASE_URL": "llm_vision_base_url",
            "OPENCLAW_LLM_VISION_API_KEY": "llm_vision_api_key",
            "OPENCLAW_EMBED_API_KEY": "embed_api_key",
            "OPENCLAW_EMBED_BASE_URL": "embed_base_url",
            "OPENCLAW_EMBED_MODEL": "embed_model",
            "OPENCLAW_MEMORY_DB": "memory_db_path",
            "OPENCLAW_RAG_DIR": "rag_persist_dir",
            "OPENCLAW_STATE_DB": "state_db_path",
            "OPENCLAW_BUDGET": "budget_usd",
        }
        # 标准名（与 settings.py / .env 一致）作为 fallback
        fallback_mapping = {
            "LLM_API_KEY": "llm_api_key",
            "LLM_BASE_URL": "llm_base_url",
            "LLM_MODEL": "llm_model",
            "LLM_VISION_MODEL": "llm_vision_model",
            "LLM_VISION_BASE_URL": "llm_vision_base_url",
            "LLM_VISION_API_KEY": "llm_vision_api_key",
            "EMBEDDING_API_KEY": "embed_api_key",
            "EMBEDDING_BASE_URL": "embed_base_url",
            "EMBEDDING_MODEL": "embed_model",
            "MEMORY_DB_PATH": "memory_db_path",
            "RAG_VECTOR_STORE_PATH": "rag_persist_dir",
            "STATE_DB_PATH": "state_db_path",
        }
        kwargs = {}
        for env_key, field_name in {**fallback_mapping, **mapping}.items():
            if env_key in os.environ:
                val = os.environ[env_key]
                if field_name in ("budget_usd",):
                    val = float(val)  # type: ignore[assignment]
                kwargs[field_name] = val
        return cls(**kwargs)


@dataclass
class HealthReport:
    """健康检查报告。"""

    status: ServiceStatus = ServiceStatus.STOPPED
    uptime_seconds: float = 0
    components: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def is_healthy(self) -> bool:
        return self.status in (ServiceStatus.RUNNING, ServiceStatus.DEGRADED)


# ── 服务门面 ──


class ServiceFacade:
    """OpenClaw 生产级统一入口。

    管理所有基础设施层的生命周期，提供干净的外部 API。
    支持 with-statement (context manager) 自动启停。

    属性:
        config: 服务配置
        status: 当前服务状态
        llm_client: LLM 客户端 (L1)
        embedding_client: 嵌入客户端 (L2)
        rag_pipeline: RAG 流水线 (L3)
        memory_manager: 记忆管理器 (L4)
        session_manager: 会话管理器 (L6)
        cost_tracker: 成本追踪器 (L5)
        observability: 可观测性 (L5)
    """

    def __init__(
        self,
        config: ServiceConfig | None = None,
        *,
        llm_config: dict[str, Any] | None = None,
        memory_db_path: str | None = None,
        rag_persist_dir: str | None = None,
        **kwargs: Any,
    ) -> None:
        # 配置合并优先级: 显式 kwargs > config 对象 > 默认
        if config is None:
            config = ServiceConfig()

        # 允许快速编程式覆盖
        if llm_config:
            config.llm_api_key = llm_config.get("api_key", config.llm_api_key)
            config.llm_base_url = llm_config.get("base_url", config.llm_base_url)
            config.llm_model = llm_config.get("model", config.llm_model)
        if memory_db_path:
            config.memory_db_path = memory_db_path
        if rag_persist_dir:
            config.rag_persist_dir = rag_persist_dir
        for key, val in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, val)

        self.config = config
        self.status = ServiceStatus.STOPPED
        self._started_at: float = 0
        self._components: dict[str, Any] = {}

    # ── 工厂方法 ──

    @classmethod
    def from_config(cls, config_path: str) -> ServiceFacade:
        """从配置文件创建服务。"""
        config = ServiceConfig.from_file(config_path)
        config = ServiceConfig.from_env()  # 环境变量覆盖
        # 重新加载文件并合并
        file_config = ServiceConfig.from_file(config_path)
        env_config = ServiceConfig.from_env()
        merged = _merge_configs(file_config, env_config)
        return cls(config=merged)

    @classmethod
    def quick(cls, api_key: str, base_url: str = "", model: str = "gpt-4o-mini") -> ServiceFacade:
        """快速创建 — 只需 API Key。"""
        return cls(llm_config={"api_key": api_key, "base_url": base_url, "model": model})

    # ── 生命周期 ──

    def start(self) -> None:
        """启动所有基础设施层。"""
        if self.status in (ServiceStatus.RUNNING, ServiceStatus.DEGRADED):
            logger.warning("Service already running")
            return

        self.status = ServiceStatus.STARTING
        self._started_at = time.time()
        errors: list[str] = []

        # L1 - LLM Client
        try:
            from core.llm_client import BaseLLMClient, OpenAIClient
        except ImportError:
            from core.llm_client import BaseLLMClient

        try:
            if self.config.llm_api_key:
                self._llm_client = OpenAIClient(
                    api_key=self.config.llm_api_key,
                    base_url=self.config.llm_base_url or "https://api.openai.com/v1",
                    default_model=self.config.llm_model,
                )
            else:
                self._llm_client = _PlaceholderLLM("no-api-key")
            self._components["llm"] = "ok"
        except Exception as exc:
            errors.append(f"LLM: {exc}")
            self._components["llm"] = f"error: {exc}"
            self._llm_client = _PlaceholderLLM(str(exc))

        # L1b - Vision LLM Client（多模态图片输入专用，独立 endpoint）
        try:
            vision_api_key = self.config.llm_vision_api_key or self.config.llm_api_key
            vision_base_url = self.config.llm_vision_base_url or self.config.llm_base_url or "https://api.openai.com/v1"
            vision_model = self.config.llm_vision_model
            # 视觉模型与主模型不同才创建独立 client（避免重复）
            if vision_api_key and vision_model and vision_model != self.config.llm_model:
                self._vision_client = OpenAIClient(
                    api_key=vision_api_key,
                    base_url=vision_base_url,
                    default_model=vision_model,
                )
                self._components["vision"] = "ok"
            else:
                self._vision_client = None
                self._components["vision"] = "skipped"
        except Exception as exc:
            errors.append(f"Vision LLM: {exc}")
            self._components["vision"] = f"error: {exc}"
            self._vision_client = None

        # L2 - Embedding Client
        try:
            from core.embedding_client import EmbeddingClient
            embed_api_key = self.config.embed_api_key or self.config.llm_api_key
            if embed_api_key:
                try:
                    from openai import OpenAI
                    embed_openai = OpenAI(
                        api_key=embed_api_key,
                        base_url=self.config.embed_base_url or self.config.llm_base_url or "https://api.openai.com/v1",
                    )
                    self._embed_client = EmbeddingClient(client=embed_openai)
                except ImportError:
                    self._embed_client = EmbeddingClient()
            else:
                self._embed_client = EmbeddingClient()
            self._components["embedding"] = "ok"
        except Exception as exc:
            errors.append(f"Embedding: {exc}")
            self._components["embedding"] = f"degraded: {exc}"
            self._embed_client = None

        # L3 - RAG Pipeline
        try:
            from rag.pipeline import RAGPipeline
            from rag.vector_store import VectorStore
            self._rag_pipeline = RAGPipeline(
                collection_name="default",
                embed_client=self._embed_client,
                vector_store=VectorStore(
                    collection_name="default",
                    persist_path=self.config.rag_persist_dir,
                ),
            )
            self._components["rag"] = "ok"
        except Exception as exc:
            errors.append(f"RAG: {exc}")
            self._components["rag"] = f"error: {exc}"
            self._rag_pipeline = None

        # L4 - Memory
        try:
            from memory.db import MemoryDB
            from memory.memory_manager import MemoryManager
            self._memory_db = MemoryDB(db_path=self.config.memory_db_path)
            self._memory_manager = MemoryManager(
                user_id=self.config.default_user_id,
                db_path=self.config.memory_db_path,
            )
            self._components["memory"] = "ok"
        except Exception as exc:
            errors.append(f"Memory: {exc}")
            self._components["memory"] = f"error: {exc}"
            self._memory_manager = None

        # L5 - Agent Observability & Cost
        try:
            from agent.observability import get_observability
            self._observability = get_observability()
            self._components["observability"] = "ok"
        except Exception as exc:
            errors.append(f"Observability: {exc}")
            self._components["observability"] = f"error: {exc}"
            self._observability = None

        try:
            from agent.cost_tracker import CostTracker
            self._cost_tracker = CostTracker(
                session_id="service",
                budget_usd=self.config.budget_usd,
                model=self.config.llm_model,
            )
            self._components["cost_tracker"] = "ok"
        except Exception as exc:
            errors.append(f"CostTracker: {exc}")
            self._components["cost_tracker"] = f"error: {exc}"
            self._cost_tracker = None

        # L6 - Session Manager
        try:
            from business.session import SessionManager
            self._session_manager = SessionManager(db=self._memory_db)
            self._components["session"] = "ok"
        except Exception as exc:
            errors.append(f"Session: {exc}")
            self._components["session"] = f"error: {exc}"
            self._session_manager = None

        # Agent State Store
        try:
            from agent.state_store import StateStore
            self._state_store = StateStore(store_path=self.config.state_db_path)
            self._components["state_store"] = "ok"
        except Exception as exc:
            errors.append(f"StateStore: {exc}")
            self._components["state_store"] = f"error: {exc}"
            self._state_store = None

        # 最终状态
        if not errors:
            self.status = ServiceStatus.RUNNING
        elif len(errors) < 3:
            self.status = ServiceStatus.DEGRADED
        else:
            self.status = ServiceStatus.ERROR

        logger.info(
            f"ServiceFacade started [{self.status.value}]",
            extra={"components": self._components, "errors": errors},
        )

    def stop(self) -> None:
        """优雅关闭所有基础设施。"""
        self.status = ServiceStatus.STOPPING
        logger.info("ServiceFacade stopping...")

        # 持久化
        try:
            if self._cost_tracker:
                _ = self._cost_tracker.report()
        except Exception:
            pass

        try:
            if self._rag_pipeline:
                self._rag_pipeline.close()
        except Exception:
            pass

        self.status = ServiceStatus.STOPPED
        logger.info("ServiceFacade stopped")

    def shutdown(self) -> None:
        """关闭服务（stop 的别名）。"""
        self.stop()

    def health_check(self) -> HealthReport:
        """执行健康检查，返回详细报告。"""
        report = HealthReport(
            status=self.status,
            uptime_seconds=time.time() - self._started_at if self._started_at else 0,
            components=dict(self._components),
        )

        # 运行中组件自检
        if self._llm_client and hasattr(self._llm_client, "health_check"):
            try:
                self._llm_client.health_check()
                report.components["llm"] = "healthy"
            except Exception as exc:
                report.errors.append(f"LLM health: {exc}")

        # 成本统计
        if self._cost_tracker:
            try:
                rpt = self._cost_tracker.report()
                report.stats["total_tokens"] = rpt.total_tokens
                report.stats["total_cost_usd"] = round(rpt.total_cost_usd, 6)
                report.stats["total_tool_calls"] = rpt.total_tool_calls
            except Exception:
                pass

        return report

    # ── Context Manager ──

    def __enter__(self) -> ServiceFacade:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()

    # ── 属性访问 ──

    @property
    def llm_client(self) -> Any:
        return self._llm_client

    @property
    def vision_client(self) -> Any:
        return self._vision_client

    @property
    def embedding_client(self) -> Any:
        return self._embed_client

    @property
    def rag_pipeline(self) -> Any:
        return self._rag_pipeline

    @property
    def memory_manager(self) -> Any:
        return self._memory_manager

    @property
    def session_manager(self) -> Any:
        return self._session_manager

    @property
    def cost_tracker(self) -> Any:
        return self._cost_tracker

    @property
    def observability(self) -> Any:
        return self._observability

    @property
    def state_store(self) -> Any:
        return self._state_store

    # ── 业务 API ──

    def create_scenario(self, scenario_type: str, **kwargs: Any) -> Any:
        """创建业务场景应用。

        Args:
            scenario_type: general_assistant / rag_customer_service / data_analyst / code_reviewer / plan_executor
            **kwargs: 传递给 ScenarioBuilder 的额外参数

        Returns:
            ScenarioApp 实例
        """
        self._ensure_running()

        from business.scenarios import ScenarioBuilder

        # 自动注入已启动的基础设施
        kwargs.setdefault("llm_client", self._llm_client)
        kwargs.setdefault("memory_manager", self._memory_manager)
        kwargs.setdefault("cost_tracker", self._cost_tracker)
        # 只有知识库客服和规划执行场景需要 RAG pipeline
        if scenario_type in ("rag_customer_service", "plan_executor"):
            kwargs.setdefault("rag_pipeline", self._rag_pipeline)

        builder = ScenarioBuilder
        method = getattr(builder, scenario_type, None)
        if method is None:
            raise ValueError(f"未知场景类型: {scenario_type}，支持: general_assistant, rag_customer_service, data_analyst, code_reviewer, plan_executor")

        app = method(**kwargs)

        # 注入视觉模型配置（多模态图片支持）
        if getattr(app, "agent", None):
            vision_model = self._resolve_vision_model()
            if vision_model:
                app.agent.vision_model = vision_model
                app.agent.vision_llm_client = self._vision_client

        return app

    def create_agent(self, preset_name: str, **kwargs: Any) -> Any:
        """创建预置 Agent。

        Args:
            preset_name: general / rag_qa / tool_calling / data_analysis / code_review
            **kwargs: 传递给 PresetAgents 的额外参数
        """
        self._ensure_running()

        from business.presets import PresetAgents

        kwargs.setdefault("llm_client", self._llm_client)
        kwargs.setdefault("memory_manager", self._memory_manager)

        method = getattr(PresetAgents, preset_name, None)
        if method is None:
            raise ValueError(f"未知预置 Agent: {preset_name}")

        agent = method(**kwargs)

        # 注入视觉模型配置
        vision_model = self._resolve_vision_model()
        if vision_model:
            agent.vision_model = vision_model
            agent.vision_llm_client = self._vision_client

        return agent

    def run_workflow(
        self,
        workflow_name: str,
        query: str,
        agents: dict | None = None,
        **kwargs: Any,
    ) -> Any:
        """运行预设工作流。

        Args:
            workflow_name: data_analysis / code_review / rag_qa
            query: 用户查询
            agents: 自定义 Agent 字典（可选）
        """
        self._ensure_running()

        from business.workflows import (
            Workflow,
            FlowStep,
            StepType,
            build_parallel_workflow,
        )
        from business.presets import PresetAgents

        if agents is None:
            all_agents = PresetAgents.create_all(
                llm_client=self._llm_client,
                memory_manager=self._memory_manager,
                rag_pipeline=self._rag_pipeline,
            )
            agents = all_agents

        if workflow_name == "data_analysis":
            wf = build_parallel_workflow(
                "数据分析",
                [("理解需求", "data_analysis"), ("数据计算", "tool_calling")],
                agents,
                aggregator_agent_name="general",
            )
        elif workflow_name == "code_review":
            steps = [
                FlowStep("读取代码", agent_name="code_review", prompt_template="{query}", output_key="code"),
                FlowStep("审查代码", agent_name="code_review", prompt_template="审查以下代码:\n{code}", depends_on=["读取代码"]),
            ]
            wf = Workflow("代码审查", steps=steps, agents=agents)
        elif workflow_name == "rag_qa":
            steps = [
                FlowStep("检索", agent_name="rag_qa", prompt_template="{query}", output_key="context"),
                FlowStep("回答", agent_name="general", prompt_template="参考上下文回答:\n{context}\n\n问题: {query}", depends_on=["检索"]),
            ]
            wf = Workflow("知识库问答", steps=steps, agents=agents)
        else:
            raise ValueError(f"未知工作流: {workflow_name}")

        return wf.run(query)

    def chat(
        self,
        query: str,
        scenario: str = "general_assistant",
        session_id: str | None = None,
        image_data: str | None = None,
        **kwargs: Any,
    ) -> str:
        """快捷对话 — 创建场景并执行单轮对话。

        前置 / 后置自动触发内容安全护栏过滤。
        """
        from security.guardrails import get_guardrails

        guardrails = get_guardrails()

        # 输入过滤
        input_result = guardrails.filter_input(query)
        if not input_result.allowed:
            logger.warning(f"Guardrails blocked input: {input_result.reason}")
            return f"[输入已拦截] {input_result.reason}"

        app = self.create_scenario(scenario, **kwargs)
        answer = app.chat(input_result.cleaned_text, session_id=session_id, image_data=image_data)

        # 输出过滤
        output_result = guardrails.filter_output(answer)
        if output_result.reason:
            logger.info(f"Guardrails masked output: {output_result.reason} (rules: {output_result.matched_rules})")
        return output_result.cleaned_text

    def chat_with_details(
        self,
        query: str,
        scenario: str = "general_assistant",
        session_id: str | None = None,
        image_data: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """快捷对话 — 返回完整结果（含工具调用链）。

        Args:
            query: 用户输入
            scenario: 场景类型
            session_id: 会话 ID（可选）
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
        from security.guardrails import get_guardrails

        guardrails = get_guardrails()

        # 输入过滤
        input_result = guardrails.filter_input(query)
        if not input_result.allowed:
            logger.warning(f"Guardrails blocked input: {input_result.reason}")
            return {
                "answer": f"[输入已拦截] {input_result.reason}",
                "success": False,
                "steps": [],
                "tool_calls_count": 0,
                "elapsed_ms": 0.0,
                "token_usage": {},
                "error": input_result.reason,
            }

        app = self.create_scenario(scenario, **kwargs)
        result = app.chat_with_details(input_result.cleaned_text, session_id=session_id, image_data=image_data)

        # 输出过滤
        answer = result.get("answer", "")
        output_result = guardrails.filter_output(answer)
        result["answer"] = output_result.cleaned_text
        if output_result.reason:
            result["guardrails"] = {
                "masked": True,
                "reason": output_result.reason,
                "matched_rules": output_result.matched_rules,
            }
            logger.info(f"Guardrails masked output: {output_result.reason}")
        return result

    def multi_agent_chat(
        self,
        query: str,
        agent_names: list[str],
        session_id: str | None = None,
        aggregator: str = "general",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """多 Agent 并行对话 — 多个专家同时分析，LLM 综合汇总。

        Args:
            query: 用户查询
            agent_names: 要并行的 Agent 名称列表（如 ["general", "data_analysis", "code_review"]）
            session_id: 会话 ID（可选）
            aggregator: 汇总 Agent 名称（默认 general）

        Returns:
            {
                "final_answer": str,          # LLM 综合后的最终答案
                "fusion_strategy": str,       # "llm" / "concat" / "none"
                "individual_results": {      # 每个 Agent 的独立结果
                    "agent_name": {
                        "answer": str,
                        "success": bool,
                        "elapsed_ms": float,
                        "tool_calls_count": int,
                        "error": str | None,
                    },
                    ...
                },
                "success": bool,              # 是否有 Agent 成功
                "elapsed_ms": float,          # 总耗时（含并行执行 + 汇总）
            }
        """
        self._ensure_running()

        from concurrent.futures import ThreadPoolExecutor, as_completed
        from business.presets import PresetAgents
        from agent.base import AgentResult
        from agent.fusion import ResultFusion, FusionStrategy
        from agent.executor import AgentExecutor

        t0 = time.perf_counter()

        # 创建所有预置 Agent
        all_agents = PresetAgents.create_all(
            llm_client=self._llm_client,
            memory_manager=self._memory_manager,
            rag_pipeline=self._rag_pipeline,
        )

        # 并行执行 — 每个 Agent 独立处理同一查询
        individual_results: dict[str, AgentResult] = {}
        with ThreadPoolExecutor(max_workers=min(len(agent_names), 8)) as executor:
            futures: dict[Any, str] = {}
            for name in agent_names:
                if name in all_agents:
                    agent = all_agents[name]
                    fut = executor.submit(AgentExecutor.run_safe, agent, query)
                    futures[fut] = name
                else:
                    individual_results[name] = AgentResult(
                        error=f"未找到 Agent: {name}",
                    )

            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    result = fut.result(timeout=self.config.agent_step_timeout)
                    individual_results[name] = result
                except Exception as exc:
                    individual_results[name] = AgentResult(
                        error=f"执行异常: {exc}",
                    )

        # 汇总 — 用 LLM 综合多个专家意见
        valid_results = {k: v for k, v in individual_results.items() if v.success and v.answer}
        fusion_strategy = "none"
        fusion_token_usage: dict[str, int] = {}
        if valid_results:
            fusion = ResultFusion.fuse(
                valid_results,
                strategy=FusionStrategy.LLM_SYNTHESIZE,
                llm_client=self._llm_client,
            )
            final_answer = fusion.answer
            fusion_strategy = fusion.strategy.value
            fusion_token_usage = fusion.token_usage
        else:
            final_answer = "所有 Agent 执行失败，无可用结果。"

        elapsed = (time.perf_counter() - t0) * 1000

        # 记录各 Agent 的 token 消耗到 cost_tracker
        if self._cost_tracker:
            for name, res in individual_results.items():
                if res.token_usage:
                    self._cost_tracker.record_tokens(
                        agent_name=name,
                        prompt_tokens=res.token_usage.get("prompt", 0),
                        completion_tokens=res.token_usage.get("completion", 0),
                    )
                if res.tool_calls_count:
                    for _ in range(res.tool_calls_count):
                        self._cost_tracker.record_tool_call(agent_name=name)
            # 记录 LLM 融合阶段的 token
            if fusion_token_usage:
                self._cost_tracker.record_tokens(
                    agent_name="fusion_llm",
                    prompt_tokens=fusion_token_usage.get("prompt", 0),
                    completion_tokens=fusion_token_usage.get("completion", 0),
                )

        return {
            "final_answer": final_answer,
            "fusion_strategy": fusion_strategy,  # llm / concat / none
            "individual_results": {
                k: {
                    "answer": v.answer,
                    "success": v.success,
                    "elapsed_ms": v.total_elapsed_ms,
                    "tool_calls_count": v.tool_calls_count,
                    "error": v.error,
                }
                for k, v in individual_results.items()
            },
            "success": any(v.success for v in individual_results.values()),
            "elapsed_ms": elapsed,
        }

    def master_agent_chat(
        self,
        query: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """主 Agent 编排模式 — 主 Agent 自行分解任务并委派子 Agent。

        与 multi_agent_chat（并行所有人）不同，此模式由 MasterOrchestrator
        在 ReAct 循环中通过 delegate_task 工具动态决定何时委派、委派给谁。

        Returns:
            {
                "final_answer": str,
                "success": bool,
                "elapsed_ms": float,
                "tool_calls_count": int,
                "steps": list,  # 主 Agent 的思考→委派→观察 步骤
            }
        """
        self._ensure_running()

        from business.presets import PresetAgents
        from agent.executor import AgentExecutor

        t0 = time.perf_counter()

        master = PresetAgents.master(
            llm_client=self._llm_client,
            memory_manager=self._memory_manager,
            rag_pipeline=self._rag_pipeline,
        )

        result = AgentExecutor.run(master, query)

        elapsed = (time.perf_counter() - t0) * 1000

        # 记录 Master Agent 的 token 消耗到 cost_tracker
        if self._cost_tracker and result.token_usage:
            self._cost_tracker.record_tokens(
                agent_name=master.name,
                prompt_tokens=result.token_usage.get("prompt", 0),
                completion_tokens=result.token_usage.get("completion", 0),
            )
        if self._cost_tracker and result.tool_calls_count:
            for _ in range(result.tool_calls_count):
                self._cost_tracker.record_tool_call(agent_name=master.name)

        return {
            "final_answer": result.answer,
            "success": result.success,
            "elapsed_ms": elapsed,
            "tool_calls_count": result.tool_calls_count,
            "token_usage": dict(result.token_usage) if result.token_usage else {},
            "steps": [
                {
                    "step_num": s.step_num,
                    "thought": s.thought,
                    "action": s.action,
                    "action_input": s.action_input,
                    "observation": s.observation,
                }
                for s in result.steps
            ],
        }

    def master_agent_chat_stream(self, query: str, **kwargs: Any):
        """主 Agent 编排模式（流式版本）— 通过 step_callback 逐步推送进度。

        这是一个生成器，每步 yield 一个事件 dict:
            {"type": "think", ...}
            {"type": "delegate", ...}
            {"type": "tool_result", ...}
            {"type": "final", ...}

        Usage:
            for event in facade.master_agent_chat_stream(query):
                render_event(event)  # 实时更新 UI
        """
        import queue
        self._ensure_running()

        from business.presets import PresetAgents
        from agent.executor import AgentExecutor

        event_queue: queue.Queue = queue.Queue()

        master = PresetAgents.master(
            llm_client=self._llm_client,
            memory_manager=self._memory_manager,
            rag_pipeline=self._rag_pipeline,
        )

        def on_step(event: dict) -> None:
            event_queue.put(event)

        t0 = time.perf_counter()
        errors: list[str] = []

        def _run_in_thread() -> None:
            try:
                result = AgentExecutor.run(master, query, step_callback=on_step)
                event_queue.put({"type": "_result", "result": result})
            except Exception as exc:
                errors.append(str(exc))
                event_queue.put({"type": "_error", "error": str(exc)})

        import threading
        t = threading.Thread(target=_run_in_thread, daemon=True)
        t.start()

        # 逐步 yield 事件，直到收到最终结果
        while True:
            try:
                event = event_queue.get(timeout=0.2)
            except queue.Empty:
                if not t.is_alive() and event_queue.empty():
                    break
                # 发送心跳，保持 UI 响应
                yield {"type": "waiting"}
                continue

            if event["type"] == "_result":
                result = event["result"]
                # 记录成本
                if self._cost_tracker and result.token_usage:
                    self._cost_tracker.record_tokens(
                        agent_name=master.name,
                        prompt_tokens=result.token_usage.get("prompt", 0),
                        completion_tokens=result.token_usage.get("completion", 0),
                    )
                if self._cost_tracker and result.tool_calls_count:
                    for _ in range(result.tool_calls_count):
                        self._cost_tracker.record_tool_call(agent_name=master.name)
                # 发送最终汇总
                elapsed = (time.perf_counter() - t0) * 1000
                yield {
                    "type": "_done",
                    "answer": result.answer,
                    "success": result.success,
                    "elapsed_ms": elapsed,
                    "tool_calls_count": result.tool_calls_count,
                    "token_usage": dict(result.token_usage) if result.token_usage else {},
                    "steps": [
                        {"step_num": s.step_num, "thought": s.thought,
                         "action": s.action, "action_input": getattr(s, "action_input", None),
                         "observation": s.observation}
                        for s in result.steps
                    ],
                }
                break
            elif event["type"] == "_error":
                yield {"type": "_error", "error": event["error"]}
                break
            else:
                yield event

    def get_prompt(self, category: str, name: str) -> Any:
        """获取提示词模板。"""
        from business.prompts import PromptLibrary
        return PromptLibrary.get(category, name)

    # ── 内部 ──

    def _ensure_running(self) -> None:
        if self.status not in (ServiceStatus.RUNNING, ServiceStatus.DEGRADED):
            self.start()

    # ── 内部：视觉模型解析 ──

    def _resolve_vision_model(self) -> str | None:
        """解析视觉模型配置。返回视觉模型名，未配置或与主模型相同则返回 None。"""
        model = self.config.llm_vision_model
        if not model:
            return None
        # 视觉模型与主模型相同时不切换（使用主 client 即可）
        if model == self.config.llm_model:
            return None
        return model


# ── 辅助 ──


class _PlaceholderLLM:
    """占位 LLM — 在无 API Key 时仍可测试业务逻辑。"""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def chat(self, messages: list, **kwargs: Any) -> tuple[str, Any]:
        return f"[PlaceholderLLM: {self._reason}]", None

    def chat_raw(self, messages: list, **kwargs: Any) -> dict[str, Any]:
        return {"content": f"[PlaceholderLLM: {self._reason}]", "tool_calls": None, "usage": None}


def _merge_configs(base: ServiceConfig, override: ServiceConfig) -> ServiceConfig:
    """合并两个配置，override 中的非空/非默认值覆盖 base。"""
    merged = ServiceConfig()
    for field_name in ServiceConfig.__dataclass_fields__:
        base_val = getattr(base, field_name)
        over_val = getattr(override, field_name)
        # 如果 override 值不同于默认值，使用它
        default_val = getattr(ServiceConfig(), field_name)
        if over_val != default_val:
            setattr(merged, field_name, over_val)
        else:
            setattr(merged, field_name, base_val)
    return merged
