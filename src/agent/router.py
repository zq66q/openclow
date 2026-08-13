"""智能路由与多 Agent 协作 — 生产级 v2。

核心类：
  AgentRouter   — 关键词 + LLM + 优先级混合路由
  AgentTeam     — 真并行多 Agent 协作（任务分解 + 并发执行 + 结果合成）
  LLMRouter     — 基于 LLM 语义理解的智能路由

增强特性（v2）:
  - AgentTeam: 真并行（ThreadPoolExecutor），不再串行
  - 任务分解: lead agent 自动将复杂 query 拆分为子任务
  - 结果合成: 自动汇总所有 worker 的结果
  - 依赖注入: worker 自动继承 lead 的 llm/memory/rag
"""

from __future__ import annotations

import concurrent.futures
import threading
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from agent.base import AgentResult
from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent


class RouteStrategy(str, Enum):
    """路由策略。"""

    KEYWORD = "keyword"
    LLM = "llm"
    PRIORITY = "priority"
    HYBRID = "hybrid"


@dataclass
class RouteDecision:
    """路由决策记录。"""

    agent_name: str
    strategy: RouteStrategy
    confidence: float = 0.0
    reason: str = ""


# ====================================================================
# AgentRouter
# ====================================================================


class AgentRouter:
    """混合路由：根据 query 内容匹配最合适的 Agent。"""

    HYBRID_THRESHOLD = 3

    def __init__(self, agents: list[BaseAgent]) -> None:
        if not agents:
            raise ValueError("AgentRouter 需要至少一个 Agent")
        self._agents = agents
        self._fallback = agents[0]
        self._keywords: dict[str, list[str]] = {}
        for agent in agents:
            keywords = [agent.name.lower(), *agent.tools]
            prompt = agent.system_prompt
            for n in (2, 3, 4):
                for i in range(len(prompt) - n + 1):
                    chunk = prompt[i : i + n]
                    if all("\u4e00" <= c <= "\u9fff" for c in chunk):
                        keywords.append(chunk)
            for word in prompt.lower().split():
                clean = word.strip('().,;:"')
                if len(clean) >= 2:
                    keywords.append(clean)
            self._keywords[agent.name] = keywords

    def route(
        self,
        query: str,
        strategy: RouteStrategy = RouteStrategy.KEYWORD,
    ) -> BaseAgent:
        """将 query 路由到最匹配的 Agent。"""
        if len(self._agents) == 1:
            return self._agents[0]

        query_lower = query.lower()

        if strategy == RouteStrategy.LLM:
            return self._llm_route(query)

        if strategy == RouteStrategy.HYBRID:
            decision = self._keyword_route(query_lower)
            if decision.confidence >= self.HYBRID_THRESHOLD:
                return self._find_agent(decision.agent_name) or self._fallback
            return self._llm_route(query)

        decision = self._keyword_route(query_lower)
        chosen = self._find_agent(decision.agent_name) or self._fallback
        logger.info(f"Router: '{query[:50]}...' -> [{chosen.name}] (confidence={decision.confidence})")
        return chosen

    def route_with_decision(self, query: str) -> RouteDecision:
        if len(self._agents) == 1:
            return RouteDecision(
                agent_name=self._agents[0].name, strategy=RouteStrategy.KEYWORD, confidence=1.0, reason="唯一 Agent"
            )
        return self._keyword_route(query.lower())

    def _keyword_route(self, query_lower: str) -> RouteDecision:
        best_agent = self._fallback
        best_score = 0
        for agent in self._agents:
            score = self._score(agent, query_lower)
            if score > best_score:
                best_score = score
                best_agent = agent
        return RouteDecision(
            agent_name=best_agent.name,
            strategy=RouteStrategy.KEYWORD,
            confidence=float(best_score),
            reason=f"关键词匹配得分 {best_score}",
        )

    def _llm_route(self, query: str) -> BaseAgent:
        try:
            llm = self._fallback.llm_client
        except Exception:
            decision = self._keyword_route(query.lower())
            return self._find_agent(decision.agent_name) or self._fallback

        agent_list = "\n".join(f"- {a.name}: {a.system_prompt[:100]}" for a in self._agents)
        route_prompt = (
            "你是一个路由决策器。根据用户问题选择最合适的 Agent。\n\n"
            f"可用 Agent:\n{agent_list}\n\n"
            f"用户问题: {query}\n\n"
            "请只回复最合适的 Agent 的名字，不要解释。"
        )
        try:
            reply, _usage = llm.chat([{"role": "user", "content": route_prompt}], temperature=0)
            reply = reply.strip()
            for agent in self._agents:
                if agent.name.lower() == reply.lower():
                    return agent
        except Exception as exc:
            logger.error(f"LLM Router error: {exc}")
        return self._fallback

    def _score(self, agent: BaseAgent, query_lower: str) -> int:
        score = 0
        for kw in self._keywords.get(agent.name, []):
            if kw in query_lower:
                score += len(kw)
        return score

    def _find_agent(self, name: str) -> BaseAgent | None:
        for a in self._agents:
            if a.name == name:
                return a
        return None


# ====================================================================
# AgentTeam — 真并行多 Agent 协作
# ====================================================================


# 子任务
@dataclass
class SubTask:
    """由 lead agent 分解出的子任务。"""

    task_id: str = ""
    description: str = ""
    assigned_agent: str = ""  # 指定执行者名称，"" 表示自动路由


class AgentTeam:
    """多 Agent 协作团队（生产级 v2）。

    执行流程:
        1. lead agent 分析 query，拆分为若干子任务（可选）
        2. 子任务并发分发到 workers（ThreadPoolExecutor）
        3. lead agent 汇总合成最终答案

    用法:
        team = AgentTeam(lead_agent, [worker1, worker2, worker3])
        result = team.run("同时查天气、汇率并对比分析")
    """

    def __init__(
        self,
        lead: BaseAgent,
        workers: list[BaseAgent] | None = None,
        *,
        max_workers: int = 5,
        parallel: bool = True,
    ) -> None:
        self._lead = lead
        self._workers = workers or []
        self._router = AgentRouter(self._workers) if self._workers else None
        self._max_workers = max_workers
        self._parallel = parallel

        # 指标
        self._total_executions = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def lead(self) -> BaseAgent:
        return self._lead

    @property
    def workers(self) -> list[BaseAgent]:
        return self._workers

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def run(self, query: str, *, decompose: bool = False) -> AgentResult:
        """主入口。

        Args:
            query: 用户输入
            decompose: 是否先让 lead 拆解任务再分发（更智能但多一次 LLM 调用）

        Returns:
            综合后的 AgentResult
        """
        with self._lock:
            self._total_executions += 1

        logger.info(
            f"AgentTeam.run lead=[{self._lead.name}] "
            f"workers={[w.name for w in self._workers]} "
            f"parallel={self._parallel} decompose={decompose}"
        )

        # 如果没有 workers，直接用 lead
        if not self._workers:
            return self._lead.run(query)

        if decompose:
            return self._run_decomposed(query)
        else:
            return self._run_parallel(query)

    def _run_parallel(self, query: str) -> AgentResult:
        """并行执行：所有 workers 同时处理同一 query，lead 合成结果。"""
        # 注入依赖
        self._inject_dependencies()

        results: dict[str, AgentResult] = {}

        if self._parallel:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                futures: dict[concurrent.futures.Future, str] = {}
                for worker in self._workers:
                    future = executor.submit(self._safe_run, worker, query)
                    futures[future] = worker.name

                for future in concurrent.futures.as_completed(futures):
                    name = futures[future]
                    try:
                        results[name] = future.result(timeout=120)
                    except Exception as exc:
                        logger.error(f"Worker [{name}] failed: {exc}")
                        results[name] = AgentResult(error=str(exc))
        else:
            # 串行回退
            for worker in self._workers:
                results[worker.name] = self._safe_run(worker, query)

        return self._synthesize(query, results)

    def _run_decomposed(self, query: str) -> AgentResult:
        """分解执行：lead 拆解任务 → 并发执行子任务 → lead 合成。"""
        # Step 1: lead 分解任务
        subtasks = self._decompose_task(query)
        if not subtasks:
            # 分解失败，回退到并行模式
            logger.warning("AgentTeam: task decomposition failed, falling back to parallel")
            return self._run_parallel(query)

        logger.info(f"AgentTeam: decomposed into {len(subtasks)} subtasks")

        # Step 2: 并发执行子任务
        self._inject_dependencies()
        sub_results: dict[str, str] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures: dict[concurrent.futures.Future, tuple[str, str]] = {}

            for st in subtasks:
                worker = self._pick_worker(st.assigned_agent)
                if worker is None:
                    continue
                task_id = st.task_id or st.description[:20]
                future = executor.submit(self._safe_run, worker, st.description)
                futures[future] = (task_id, worker.name)

            for future in concurrent.futures.as_completed(futures):
                task_id, worker_name = futures[future]
                try:
                    result = future.result(timeout=120)
                    sub_results[task_id] = result.answer if result.success else f"[错误: {result.error}]"
                except Exception as exc:
                    sub_results[task_id] = f"[超时/异常: {exc}]"

        # Step 3: lead 合成
        return self._synthesize_from_subtasks(query, sub_results)

    @staticmethod
    def _safe_run(agent: BaseAgent, query: str) -> AgentResult:
        """安全执行单个 Agent（捕获异常）。"""
        try:
            return agent.run(query)
        except Exception as exc:
            return AgentResult(error=str(exc))

    # ------------------------------------------------------------------
    # 任务分解
    # ------------------------------------------------------------------

    def _decompose_task(self, query: str) -> list[SubTask]:
        """让 lead agent 将复杂 query 分解为子任务。"""
        worker_list = "\n".join(f"- {w.name}: {w.system_prompt[:80]}" for w in self._workers)
        decompose_prompt = (
            "你是一个任务规划器。请将用户的复杂问题分解为若干个独立的子任务，"
            "每个子任务适合分配给一个专门的下属 Agent 处理。\n\n"
            f"下属 Agent:\n{worker_list}\n\n"
            f"用户问题: {query}\n\n"
            "请以 JSON 格式返回子任务列表:\n"
            '[{"task_id": "编号", "description": "子任务描述", "assigned_agent": "Agent名称"}]'
        )

        try:
            import json

            reply, _ = self._lead.llm_client.chat(
                [{"role": "user", "content": decompose_prompt}],
                temperature=0.2,
            )
            # 提取 JSON 数组
            json_match = __import__("re").search(r"\[.*\]", reply, __import__("re").DOTALL)
            if json_match:
                raw_tasks = json.loads(json_match.group(0))
                return [
                    SubTask(
                        task_id=t.get("task_id", str(i)),
                        description=t.get("description", ""),
                        assigned_agent=t.get("assigned_agent", ""),
                    )
                    for i, t in enumerate(raw_tasks)
                ]
        except Exception as exc:
            logger.warning(f"Task decomposition failed: {exc}")

        return []

    def _pick_worker(self, agent_name: str) -> BaseAgent | None:
        """根据名称选择 worker。"""
        if not agent_name:
            return self._workers[0] if self._workers else None
        for w in self._workers:
            if w.name.lower() == agent_name.lower():
                return w
        return self._workers[0] if self._workers else None

    # ------------------------------------------------------------------
    # 结果合成
    # ------------------------------------------------------------------

    def _synthesize(self, query: str, results: dict[str, AgentResult]) -> AgentResult:
        """将多个 worker 的并行结果合成为最终答案。"""
        success_count = sum(1 for r in results.values() if r.success)

        if success_count == 0:
            errors = "; ".join(r.error or "未知错误" for r in results.values())
            return AgentResult(error=f"所有 Worker 执行失败: {errors}")

        # 让 lead 合成
        answers_text = "\n\n".join(f"### [{name}]\n{r.answer}" for name, r in results.items() if r.success)
        synthesize_prompt = (
            "你是一个信息综合器。以下是多个 Agent 对同一问题的回答。"
            "请综合所有信息，给出一个完整、精炼的最终答案。\n\n"
            f"原始问题: {query}\n\n"
            f"各 Agent 的回答:\n{answers_text}\n\n"
            "最终答案:"
        )

        try:
            reply, usage = self._lead.llm_client.chat(
                [{"role": "user", "content": synthesize_prompt}],
                temperature=0.3,
            )
            return AgentResult(
                answer=reply.strip(),
                loop_type="team_synthesis",
                token_usage={
                    "prompt": usage.prompt_tokens if usage is not None else 0,
                    "completion": usage.completion_tokens if usage is not None else 0,
                    "total": usage.total_tokens if usage is not None else 0,
                },
            )
        except Exception:
            # LLM 合成失败，直接拼接
            parts = [f"【{name}】\n{r.answer}" for name, r in results.items() if r.success]
            return AgentResult(
                answer="\n\n".join(parts),
                loop_type="team_concat",
            )

    def _synthesize_from_subtasks(self, query: str, sub_results: dict[str, str]) -> AgentResult:
        """合成分解任务的结果。"""
        answers_text = "\n\n".join(f"### 子任务 [{tid}]\n{answer}" for tid, answer in sub_results.items())
        synthesize_prompt = (
            "你是一个任务综合器。以下是各个子任务的执行结果。"
            "请综合所有子任务的结果，给出一个完整、条理清晰的最终答案。\n\n"
            f"原始问题: {query}\n\n"
            f"子任务结果:\n{answers_text}\n\n"
            "最终答案:"
        )

        try:
            reply, usage = self._lead.llm_client.chat(
                [{"role": "user", "content": synthesize_prompt}],
                temperature=0.3,
            )
            return AgentResult(
                answer=reply.strip(),
                loop_type="team_decomposed_synthesis",
                token_usage={
                    "prompt": usage.prompt_tokens if usage is not None else 0,
                    "completion": usage.completion_tokens if usage is not None else 0,
                    "total": usage.total_tokens if usage is not None else 0,
                },
            )
        except Exception:
            parts = [f"【{tid}】\n{a}" for tid, a in sub_results.items()]
            return AgentResult(answer="\n\n".join(parts), loop_type="team_decomposed_concat")

    # ------------------------------------------------------------------
    # 其他方法
    # ------------------------------------------------------------------

    def run_with_router(self, query: str) -> AgentResult:
        """自动路由到最合适的 worker 执行。"""
        if not self._workers or not self._router:
            return self._lead.run(query)
        chosen = self._router.route(query)
        self._inject_to(chosen)
        return chosen.run(query)

    def run_all(self, query: str) -> dict[str, AgentResult]:
        """让所有 workers 执行同一 query（用于对比/投票场景）。"""
        results: dict[str, AgentResult] = {}
        self._inject_dependencies()

        if self._parallel:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                futures = {executor.submit(self._safe_run, w, query): w.name for w in self._workers}
                for future in concurrent.futures.as_completed(futures):
                    name = futures[future]
                    try:
                        results[name] = future.result(timeout=120)
                    except Exception as exc:
                        results[name] = AgentResult(error=str(exc))
        else:
            for worker in self._workers:
                results[worker.name] = self._safe_run(worker, query)

        return results

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _inject_dependencies(self) -> None:
        """把所有 worker 注入 lead 的依赖。"""
        for worker in self._workers:
            self._inject_to(worker)

    def _inject_to(self, worker: BaseAgent) -> None:
        """注入依赖到单个 worker。"""
        if not worker._llm_client:
            with suppress(Exception):
                worker._llm_client = self._lead.llm_client
        if not worker.memory:
            worker._memory = self._lead.memory
        if not worker.rag:
            worker._rag = self._lead.rag
