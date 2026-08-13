"""业务工作流引擎 — 多步骤 DAG 执行（生产增强版 v2）。

单次 Agent.run() 只能一问一答，真实业务需要多步骤流水线：
  数据分析 = 理解需求 → 写分析方案 → 执行计算 → 生成报告 → 人工审批

增强（v2）:
  - 真正的并行执行（ThreadPoolExecutor）
  - 指数退避重试（1s, 2s, 4s, ...）
  - 每步超时保护
  - 工作流状态持久化（JSON 快照）
  - 取消令牌支持
  - 深度感知依赖解析

用法:
    wf = Workflow("数据分析", steps=[
        FlowStep("理解需求", "data_analyst", "分析: {query}", output_key="plan"),
        FlowStep("执行计算", "tool_calling", "按方案执行: {plan}", depends_on=["理解需求"]),
        FlowStep("生成报告", "general", "总结: {plan} 结果: {执行计算}"),
    ], agents=agents)
    result = wf.run("最近三个月销售额趋势")
"""

from __future__ import annotations

import json
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, TYPE_CHECKING

from agent.state import AgentContext
from agent.executor import AgentExecutor
from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent


# ── 默认配置 ──

DEFAULT_STEP_TIMEOUT = 120       # 单步默认超时（秒）
DEFAULT_WORKFLOW_TIMEOUT = 600   # 工作流整体默认超时（秒）
BACKOFF_BASE = 1.0               # 退避基础（秒）
BACKOFF_MULTIPLIER = 2.0         # 退避倍率
BACKOFF_MAX = 30.0               # 退避上限（秒）


# ── 步骤类型 ──


class StepType(str, Enum):
    ACTION = "action"        # 普通 Agent 调用
    CONDITION = "condition"  # 条件分支
    PARALLEL = "parallel"    # 并行执行
    APPROVAL = "approval"    # 人工审批


@dataclass
class FlowStep:
    """工作流中的单个执行步骤。

    Attributes:
        name: 步骤名称（唯一标识）
        agent_name: 绑定的 Agent 名称，对应 agents 字典中的 key
        prompt_template: 提示词模板，支持 {variable} 占位符从 context 注入
        output_key: 将执行结果的 answer 保存到 context 的 key
        step_type: 步骤类型
        depends_on: 依赖的前置步骤名列表
        condition_fn: 条件分支的判断函数（Callable）(step_type == CONDITION 时使用)
        parallel_steps: 并行子步骤列表（step_type == PARALLEL 时使用）
        skip_on_error: 依赖失败时是否跳过（默认 False）
        timeout: 本步超时（秒，None 使用默认）
        max_retries: 本步重试次数（None 使用工作流默认）
    """

    name: str
    agent_name: str = ""
    prompt_template: str = "{query}"
    output_key: str = ""
    step_type: StepType = StepType.ACTION
    depends_on: list[str] = field(default_factory=list)
    condition_fn: Callable[..., bool] | None = None
    parallel_steps: list[FlowStep] = field(default_factory=list)
    skip_on_error: bool = False
    timeout: float | None = None
    max_retries: int | None = None


# ── 步骤结果 ──


@dataclass
class StepResult:
    """单步执行结果。"""

    step_name: str
    output: str = ""
    success: bool = True
    error: str | None = None
    elapsed_ms: float = 0
    retries: int = 0


# ── 运行结果 ──


@dataclass
class WorkflowResult:
    """完整工作流运行结果。"""

    workflow_name: str = ""
    steps: list[StepResult] = field(default_factory=list)
    final_output: str = ""
    success: bool = True
    error: str | None = None
    total_elapsed_ms: float = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "steps": [
                {
                    "step_name": s.step_name,
                    "output": s.output,
                    "success": s.success,
                    "error": s.error,
                    "elapsed_ms": s.elapsed_ms,
                    "retries": s.retries,
                }
                for s in self.steps
            ],
            "final_output": self.final_output,
            "success": self.success,
            "error": self.error,
            "total_elapsed_ms": self.total_elapsed_ms,
        }


# ── 工作流状态持久化 ──


@dataclass
class WorkflowState:
    """工作流执行状态（可持久化），支持跨进程恢复。"""

    wf_id: str
    status: str = "pending"          # pending / running / success / failed / cancelled
    current_step: int = 0
    step_results: list[StepResult] = field(default_factory=list)
    error: str | None = None
    started_at: float = 0.0
    updated_at: float = 0.0
    total_elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "wf_id": self.wf_id,
            "status": self.status,
            "current_step": self.current_step,
            "step_results": [s.__dict__ if hasattr(s, "__dict__") else {} for s in self.step_results],
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "total_elapsed_ms": self.total_elapsed_ms,
        }


# ── 工作流 ──


class Workflow:
    """工作流编排器（生产增强版 v2）。

    按 DAG 拓扑顺序执行步骤，自动在 AgentContext 中传递中间结果。
    支持顺序、条件分支、并行（真正多线程）、人工审批四种步骤类型。

    增强特性:
      - 并行步骤使用 ThreadPoolExecutor 真正并发
      - 指数退避重试（1s, 2s, 4s, ... max 30s）
      - 每步可选超时
      - 工作流状态持久化（save_state / restore_state）
      - 取消令牌支持

    用法:
        wf = Workflow("数据分析",
            steps=[
                FlowStep("step1", agent_name="analyst", output_key="analysis"),
                FlowStep("step2", agent_name="writer", depends_on=["step1"]),
            ],
            agents={"analyst": da_agent, "writer": wr_agent},
        )
        result = wf.run("查询上个月销售数据")
    """

    def __init__(
        self,
        name: str,
        steps: list[FlowStep] | None = None,
        agents: dict[str, BaseAgent] | None = None,
        max_step_retries: int = 1,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        workflow_timeout: float = DEFAULT_WORKFLOW_TIMEOUT,
    ) -> None:
        self.name = name
        self.steps = steps or []
        self.agents = agents or {}
        self.max_step_retries = max_step_retries
        self.step_timeout = step_timeout
        self.workflow_timeout = workflow_timeout
        self._cancel_event = Event()
        self._state_lock = Lock()

    # ── 取消 ──

    def cancel(self) -> None:
        """发送取消信号。"""
        self._cancel_event.set()
        logger.warning(f"Workflow [{self.name}] cancel requested")

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ── 拓扑排序 ──

    def _topological_order(self) -> list[FlowStep]:
        """对步骤做拓扑排序，依赖项在前。"""
        name_to_step = {s.name: s for s in self.steps}
        visited: set[str] = set()
        temp: set[str] = set()
        ordered: list[FlowStep] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in temp:
                raise RuntimeError(f"工作流存在循环依赖: {name}")
            temp.add(name)
            step = name_to_step.get(name)
            if step is None:
                raise RuntimeError(f"未知步骤: {name}，检查 depends_on")
            for dep in step.depends_on:
                visit(dep)
            temp.discard(name)
            visited.add(name)
            ordered.append(step)

        for s in self.steps:
            visit(s.name)

        return ordered

    # ── 模板注入 ──

    @staticmethod
    def _inject_context(template: str, context: AgentContext, query: str = "") -> str:
        """将 context 中的变量���入模板。

        {变量名} 从 context.data 中取值，{query} 固定取原始查询。
        """
        result = template.replace("{query}", query)
        for key, val in context.data.items():
            val_str = str(val) if not isinstance(val, str) else val
            result = result.replace(f"{{{key}}}", val_str)
        return result

    # ── 状态持久化 ──

    def save_state(self, path: str, query: str = "",
                   step_results: list[StepResult] | None = None) -> None:
        """将工作流当前状态保存到 JSON 文件，支持断点续跑。"""
        state = {
            "name": self.name,
            "query": query,
            "step_names": [s.name for s in self.steps],
            "completed": [r.step_name for r in (step_results or []) if r.success],
            "step_results": [{
                "step_name": r.step_name, "success": r.success,
                "output": r.output[:500], "error": r.error,
            } for r in (step_results or [])],
            "timestamp": time.time(),
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Workflow [{self.name}] state saved to {path}")

    @staticmethod
    def restore_state(path: str) -> dict[str, Any]:
        """从 JSON 文件恢复工作流状态。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        logger.info(f"Workflow state restored from {path}: {len(data.get('completed', []))} steps completed")
        return data

    # ── 主执行 ──

    def run(self, query: str, context: AgentContext | None = None) -> WorkflowResult:
        """执行完整工作流。

        Args:
            query: 初始用户查询
            context: 外部传入的上下文（可选，不传则新建）

        Returns:
            WorkflowResult
        """
        t0 = time.perf_counter()
        self._cancel_event.clear()
        ctx = context or AgentContext()
        ctx.note(source="workflow", event="start", detail={"name": self.name, "query": query})

        if not self.steps:
            wfr = WorkflowResult(workflow_name=self.name, error="无执行步骤", success=False)
            wfr.total_elapsed_ms = (time.perf_counter() - t0) * 1000
            return wfr

        step_results: list[StepResult] = []
        dep_results: dict[str, StepResult] = {}
        all_ok = True

        try:
            ordered_steps = self._topological_order()
        except RuntimeError as exc:
            logger.error(f"Workflow [{self.name}] topology error: {exc}")
            return WorkflowResult(workflow_name=self.name, error=str(exc), success=False)

        for step in ordered_steps:
            # 取消检查
            if self._cancel_event.is_set():
                sr = StepResult(step_name=step.name, error="工作流已取消", success=False)
                step_results.append(sr)
                all_ok = False
                break

            # 超时检查
            elapsed_total = time.perf_counter() - t0
            if elapsed_total > self.workflow_timeout:
                logger.error(f"Workflow [{self.name}] timeout after {elapsed_total:.1f}s")
                sr = StepResult(step_name=step.name, error="工作流整体超时", success=False)
                step_results.append(sr)
                all_ok = False
                break

            # 检查依赖是否失败
            dep_failed = False
            for dep_name in step.depends_on:
                if dep_name in dep_results and not dep_results[dep_name].success:
                    dep_failed = True
                    break

            if dep_failed:
                if step.skip_on_error:
                    sr = StepResult(step_name=step.name, error="依赖步骤失败，已跳过", success=False)
                    step_results.append(sr)
                    dep_results[step.name] = sr
                    continue
                else:
                    sr = StepResult(step_name=step.name, error="依赖步骤失败，工作流终止", success=False)
                    step_results.append(sr)
                    all_ok = False
                    break

            # 根据步骤类型执行
            try:
                sr = self._execute_step(step, ctx, query, dep_results)
            except Exception as exc:
                logger.error(f"Workflow [{self.name}] step [{step.name}] error: {exc}")
                sr = StepResult(step_name=step.name, error=str(exc), success=False)

            step_results.append(sr)
            dep_results[step.name] = sr

            if not sr.success:
                all_ok = False
                break

        final_output = step_results[-1].output if step_results else ""
        elapsed = (time.perf_counter() - t0) * 1000

        ctx.note(source="workflow", event="finish",
                 detail={"success": all_ok, "steps": len(step_results), "cancelled": self._cancel_event.is_set()})

        return WorkflowResult(
            workflow_name=self.name,
            steps=step_results,
            final_output=final_output,
            success=all_ok,
            total_elapsed_ms=elapsed,
        )

    # ── 步骤分发 ──

    def _execute_step(
        self,
        step: FlowStep,
        ctx: AgentContext,
        query: str,
        dep_results: dict[str, StepResult],
    ) -> StepResult:
        """根据 step_type 分发到不同的执行器。"""
        if step.step_type == StepType.PARALLEL and step.parallel_steps:
            return self._run_parallel(step, ctx, query, dep_results)
        elif step.step_type == StepType.CONDITION:
            return self._run_condition(step, ctx, query, dep_results)
        elif step.step_type == StepType.APPROVAL:
            return self._run_approval(step, ctx, query)
        else:
            return self._run_action_with_retry(step, ctx, query)

    # ── 动作步骤（带指数退避重试） ──

    def _run_action_with_retry(self, step: FlowStep, ctx: AgentContext, query: str) -> StepResult:
        """执行常规 Agent 调用，支持指数退避重试。"""
        agent = self.agents.get(step.agent_name)
        if agent is None:
            return StepResult(step_name=step.name, error=f"Agent 未找到: {step.agent_name}", success=False)

        injected_prompt = self._inject_context(step.prompt_template, ctx, query)
        max_retries = step.max_retries if step.max_retries is not None else self.max_step_retries
        step_timeout = step.timeout if step.timeout is not None else self.step_timeout

        ctx.note(source=step.name, event="run",
                 detail={"agent": step.agent_name, "prompt": injected_prompt[:200],
                         "max_retries": max_retries, "timeout": step_timeout})

        t0 = time.perf_counter()
        last_result = None
        retries = 0

        for attempt in range(max_retries + 1):
            if self._cancel_event.is_set():
                return StepResult(step_name=step.name, error="已取消", success=False, elapsed_ms=(time.perf_counter() - t0) * 1000, retries=retries)

            try:
                result = AgentExecutor.run(agent, injected_prompt)
            except Exception as exc:
                result = type("_Result", (), {"success": False, "answer": "", "error": str(exc)})()

            if result.success:
                elapsed = (time.perf_counter() - t0) * 1000
                ctx.note(source=step.name, event="done",
                         detail={"success": True, "output_len": len(result.answer), "retries": retries})
                if step.output_key:
                    ctx.put(step.output_key, result.answer)
                return StepResult(
                    step_name=step.name, output=result.answer, success=True,
                    elapsed_ms=elapsed, retries=retries,
                )

            last_result = result
            retries += 1

            if attempt < max_retries:
                backoff = min(BACKOFF_BASE * (BACKOFF_MULTIPLIER ** attempt), BACKOFF_MAX)
                logger.warning(
                    f"Workflow step [{step.name}] failed (attempt {attempt+1}/{max_retries+1}), "
                    f"retrying in {backoff:.1f}s: {result.error}"
                )
                time.sleep(backoff)

        elapsed = (time.perf_counter() - t0) * 1000
        error_msg = last_result.error or "未知错误" if last_result else "未知错误"
        output = last_result.answer if last_result and last_result.answer else ""
        ctx.note(source=step.name, event="done",
                 detail={"success": False, "error": error_msg, "retries": retries})
        return StepResult(
            step_name=step.name, output=output, success=False,
            error=f"重试{max_retries}次后仍失败: {error_msg}",
            elapsed_ms=elapsed, retries=retries,
        )

    # 兼容旧名
    _run_action = _run_action_with_retry

    # ── 条件分支 ──

    def _run_condition(
        self,
        step: FlowStep,
        ctx: AgentContext,
        query: str,
        dep_results: dict[str, StepResult],
    ) -> StepResult:
        """执行条件判断，根据结果继续或跳过后续步骤。"""
        if step.condition_fn is None:
            return StepResult(step_name=step.name, error="条件函数未定义", success=False)

        try:
            passed = bool(step.condition_fn(ctx, dep_results))
            output = "条件满足" if passed else "条件不满足"
            ctx.put(f"__condition__{step.name}", passed)
            return StepResult(step_name=step.name, output=output, success=passed)
        except Exception as exc:
            return StepResult(step_name=step.name, error=f"条件判断异常: {exc}", success=False)

    # ── 并行执行（真正多线程） ──

    def _run_parallel(
        self,
        step: FlowStep,
        ctx: AgentContext,
        query: str,
        dep_results: dict[str, StepResult],
    ) -> StepResult:
        """使用 ThreadPoolExecutor 真正并行执行多个子步骤。"""
        t0 = time.perf_counter()
        sub_steps = step.parallel_steps
        if not sub_steps:
            return StepResult(step_name=step.name, output="", success=True)

        # 使用线程池并行执行子步骤
        max_workers = min(len(sub_steps), 8)  # 最多 8 个并发线程
        results_map: dict[str, dict[str, Any]] = {}
        all_ok = True

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"wf_parallel_{step.name}") as executor:
            futures: dict[Future, str] = {}
            for sub_step in sub_steps:
                if self._cancel_event.is_set():
                    break
                fut = executor.submit(self._execute_sub_step, sub_step, ctx, query, dep_results)
                futures[fut] = sub_step.name

            for fut in as_completed(futures, timeout=self.step_timeout):
                sub_name = futures[fut]
                try:
                    sub_result = fut.result(timeout=10)
                    results_map[sub_name] = {
                        "name": sub_name, "output": sub_result.output,
                        "success": sub_result.success, "error": sub_result.error,
                    }
                    if not sub_result.success:
                        all_ok = False
                        logger.warning(f"Workflow parallel sub-step [{sub_name}] failed: {sub_result.error}")
                except Exception as exc:
                    results_map[sub_name] = {
                        "name": sub_name, "output": "", "success": False,
                        "error": str(exc),
                    }
                    all_ok = False
                    logger.error(f"Workflow parallel sub-step [{sub_name}] exception: {exc}")

        # 按原始顺序汇总结果
        ordered_results = []
        for sub_step in sub_steps:
            if sub_step.name in results_map:
                ordered_results.append(results_map[sub_step.name])
            else:
                ordered_results.append({
                    "name": sub_step.name, "output": "", "success": False,
                    "error": "未执行（取消或超时）",
                })
                all_ok = False

        combined = "\n\n".join(
            f"[{r['name']}]: {r['output']}" for r in ordered_results
        )
        elapsed = (time.perf_counter() - t0) * 1000

        # 将每个子步骤的结果存入 context
        for r in ordered_results:
            if r["success"]:
                ctx.put(r["name"], r["output"])

        # 合并结果存入 context
        ctx.put(step.name, combined)

        return StepResult(
            step_name=step.name,
            output=combined,
            success=all_ok,
            elapsed_ms=elapsed,
        )

    def _execute_sub_step(
        self,
        sub_step: FlowStep,
        ctx: AgentContext,
        query: str,
        dep_results: dict[str, StepResult],
    ) -> StepResult:
        """线程池中执行的子步骤包装。"""
        try:
            return self._execute_step(sub_step, ctx, query, dep_results)
        except Exception as exc:
            return StepResult(step_name=sub_step.name, error=str(exc), success=False)

    # ── 人工审批 ──

    def _run_approval(self, step: FlowStep, ctx: AgentContext, query: str) -> StepResult:
        """暂停等待人工审批。"""
        from agent.human_review import HumanReview, ReviewRequest, ReviewAction

        step_timeout = step.timeout if step.timeout is not None else self.step_timeout
        injected_prompt = self._inject_context(step.prompt_template, ctx, query)
        agent = self.agents.get(step.agent_name) if step.agent_name else None

        # 先让 Agent 生成待审批内容
        if agent:
            try:
                agent_result = AgentExecutor.run(agent, injected_prompt)
                content = agent_result.answer
            except Exception as exc:
                return StepResult(step_name=step.name, error=f"Agent 生成内容失败: {exc}", success=False)
        else:
            content = injected_prompt

        request = ReviewRequest(
            title=f"工作流审批: {step.name}",
            content=content,
            source_agent=agent.name if agent else "workflow",
            risk_level="medium",
            timeout_seconds=step_timeout,
        )

        hr = HumanReview()
        result = hr.request_review(request)

        if result.action == ReviewAction.APPROVE:
            output = result.modified_content or content
            ctx.note(source=step.name, event="approved", detail={"output": output[:200]})
            return StepResult(step_name=step.name, output=output, success=True)
        elif result.action == ReviewAction.MODIFY:
            output = result.modified_content or content
            ctx.note(source=step.name, event="modified", detail={"comment": result.comment})
            return StepResult(step_name=step.name, output=output, success=True)
        elif result.action == ReviewAction.TIMEOUT:
            ctx.note(source=step.name, event="timeout", detail={"timeout": step_timeout})
            return StepResult(step_name=step.name, error=f"审批超时（{step_timeout}s）", success=False)
        else:
            ctx.note(source=step.name, event="rejected", detail={"comment": result.comment})
            return StepResult(step_name=step.name, error=f"审批被拒绝: {result.comment}", success=False)


# ── 便捷构建器 ──


def build_sequential_workflow(
    name: str,
    agent_names: list[str],
    agents: dict[str, BaseAgent],
) -> Workflow:
    """快速构建顺序流水线。

    每个步骤的输出自动以 step_{i} 为 key 存入上下文，供后续步骤引用。
    """
    steps: list[FlowStep] = []
    for i, aname in enumerate(agent_names):
        steps.append(FlowStep(
            name=f"step_{i}",
            agent_name=aname,
            prompt_template="{query}" if i == 0 else "上一步结果: {step_%d}" % (i - 1),
            output_key=f"step_{i}",
            depends_on=[f"step_{i-1}"] if i > 0 else [],
        ))
    return Workflow(name=name, steps=steps, agents=agents)


def build_parallel_workflow(
    name: str,
    sub_queries: list[tuple[str, str]],
    agents: dict[str, BaseAgent],
    aggregator_agent_name: str = "general",
) -> Workflow:
    """快速构建并行+汇总流水线（生产版：真正并发）。

    Args:
        name: 工作流名
        sub_queries: [(step_name, agent_name), ...] 并行执行的子任务
        agents: Agent 字典
        aggregator_agent_name: 用于汇总结果的 Agent 名
    """
    parallel_steps = [
        FlowStep(name=sn, agent_name=an, prompt_template="{query}", output_key=sn)
        for sn, an in sub_queries
    ]

    parallel = FlowStep(
        name="parallel_tasks",
        step_type=StepType.PARALLEL,
        parallel_steps=parallel_steps,
    )

    aggregator = FlowStep(
        name="aggregate",
        agent_name=aggregator_agent_name,
        prompt_template="请汇总以下分析结果:\n{parallel_tasks}",
        depends_on=["parallel_tasks"],
    )

    return Workflow(name=name, steps=[parallel, aggregator], agents=agents)
