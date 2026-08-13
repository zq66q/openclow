"""Plan-and-Execute 循环 — 先规划、再执行、后自检修正。

工作流：
  1. Plan  — LLM 将问题分解为步骤列表 (JSON)
  2. Execute — 逐步骤调用 ReAct 循环完成任务
  3. Review — LLM 检查结果，判断是否需要修正/补充
  4. 未完成 → 回到 Plan（最多 2 轮重规划）
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from agent.base import AgentResult, AgentState
from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent


class PlanExecuteLoop:
    """Plan-and-Execute 循环（纯静态方法）。"""

    MAX_REPLAN_ROUNDS = 2  # 最大重规划轮数
    MAX_STEP_RETRIES = 1  # 单步失败重试次数

    # ── 公开入口 ──

    @staticmethod
    def run(
        agent: BaseAgent,
        query: str,
        messages: list[dict[str, Any]] | None = None,
        step_callback: callable | None = None,
        image_data: str | None = None,
    ) -> AgentResult:
        """执行 Plan-and-Execute 循环。

        Args:
            agent: 配置了 loop_type="plan_execute" 的 Agent
            query: 用户问题
            messages: 预设消息（可空，默认用 ContextBuilder 构建）
            step_callback: 步骤回调（兼容流式输出）
            image_data: base64 编码的图片数据（多模态支持，当前 Plan 阶段暂不直接处理）
        """
        start_time = time.perf_counter()
        agent.state = AgentState.RUNNING
        total_tokens = {"prompt": 0, "completion": 0, "total": 0}
        all_tool_calls = 0
        all_steps: list[dict[str, Any]] = []

        try:
            # ── Phase 1: Plan ──
            plan = PlanExecuteLoop._plan(agent, query, total_tokens)
            if step_callback:
                step_callback(
                    {
                        "type": "think",
                        "step": 0,
                        "thought": f"已生成执行计划（共 {len(plan)} 步）",
                        "action": "plan",
                        "action_input": {"plan": plan},
                    }
                )

            # ── Phase 2: Execute + Review (loop) ──
            final_answer: str = ""
            all_executed_results: list[dict[str, Any]] = []
            replan_round = 0

            while replan_round <= PlanExecuteLoop.MAX_REPLAN_ROUNDS:
                if agent.is_cancelled:
                    return AgentResult(
                        answer="（执行已被取消）",
                        loop_type="plan_execute",
                        error="cancelled",
                        total_elapsed_ms=(time.perf_counter() - start_time) * 1000,
                        token_usage=total_tokens,
                    )

                # 2a. 执行当前计划的所有步骤
                for step_item in plan:
                    if agent.is_cancelled:
                        break
                    step_result = PlanExecuteLoop._execute_step(
                        agent, step_item, total_tokens, all_tool_calls, step_callback
                    )
                    all_executed_results.append(step_result)
                    all_tool_calls += step_result.get("tool_calls", 0)

                # 2b. 步骤全部执行后，综合 Review
                review = PlanExecuteLoop._review(agent, query, plan, all_executed_results, total_tokens)

                if step_callback:
                    step_callback(
                        {
                            "type": "think",
                            "step": len(plan) + 1,
                            "thought": f"Review 结果: {review.get('status', 'unknown')}",
                            "action": "review",
                            "action_input": review,
                        }
                    )

                if review.get("complete", False):
                    final_answer = review.get("answer", "")
                    break

                # 2c. 未完成 → 重新规划
                replan_round += 1
                if replan_round <= PlanExecuteLoop.MAX_REPLAN_ROUNDS:
                    # 将已执行结果作为上下文传给 Planner
                    executed_context = PlanExecuteLoop._format_executed_results(all_executed_results)
                    extra_prompt = (
                        f"上一轮执行结果如下:\n{executed_context}\n\n"
                        f"Review 认为还需补充: {review.get('missing', '')}\n"
                        "请生成新的执行计划完成剩余工作。"
                    )
                    plan = PlanExecuteLoop._plan(agent, query, total_tokens, extra=extra_prompt)
                    if step_callback:
                        step_callback(
                            {
                                "type": "think",
                                "step": len(all_executed_results) + 2,
                                "thought": f"第 {replan_round} 轮重新规划，新增 {len(plan)} 个步骤",
                                "action": "replan",
                                "action_input": {"round": replan_round, "new_plan": plan},
                            }
                        )

            # 如果循环耗尽仍未完成，强制综合
            if not final_answer:
                final_answer = PlanExecuteLoop._force_synthesize(agent, query, all_executed_results, total_tokens)

            elapsed = (time.perf_counter() - start_time) * 1000
            agent.state = AgentState.DONE

            if step_callback:
                step_callback(
                    {
                        "type": "final",
                        "answer": final_answer,
                    }
                )

            return AgentResult(
                answer=final_answer,
                steps=all_steps,
                tool_calls_count=all_tool_calls,
                loop_type="plan_execute",
                total_elapsed_ms=elapsed,
                token_usage=total_tokens,
            )

        except Exception as exc:
            agent.state = AgentState.ERROR
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.error("PlanExecuteLoop failed", extra={"error": str(exc), "query": query[:100]})
            return AgentResult(
                answer=f"Plan-and-Execute 执行出错: {exc}",
                steps=all_steps,
                loop_type="plan_execute",
                error=str(exc),
                total_elapsed_ms=elapsed,
                token_usage=total_tokens,
            )

    # ── Phase 1: Plan ──

    @staticmethod
    def _plan(
        agent: BaseAgent,
        query: str,
        total_tokens: dict[str, int],
        extra: str = "",
    ) -> list[dict[str, Any]]:
        """让 LLM 生成执行计划。

        Returns:
            [{"step": 1, "description": "...", "tool": "tool_name", "input": "..."}, ...]
        """
        prompt = PlanExecuteLoop._build_planner_prompt(query, extra)
        messages = [{"role": "system", "content": prompt}]

        raw, usage = agent.llm_client.chat(messages, temperature=0.3)  # 低温度确保规划合理
        PlanExecuteLoop._add_tokens(total_tokens, usage)
        return PlanExecuteLoop._parse_plan(raw)

    @staticmethod
    def _build_planner_prompt(query: str, extra: str = "") -> str:
        prompt = (
            "你是一个任务规划专家。将用户问题分解为可执行的步骤列表。\n\n"
            "## 可用工具\n"
            "- web_search: 搜索网络信息\n"
            "- read_file: 读取文件内容\n"
            "- current_time: 获取当前时间\n"
            "- safe_eval: 安全计算数学表达式\n"
            "- unit_convert: 单位转换\n\n"
            "## 输出格式\n"
            "请以 JSON 数组返回计划，每个步骤包含:\n"
            "- step: 步骤编号 (整数)\n"
            "- description: 本步骤要做什么 (中文)\n"
            '- tool: 使用的工具名 (如果没有合适的工具，填 "none")\n'
            "- input: 传给工具的输入参数\n\n"
            "## 规则\n"
            "- 步骤要独立、可执行、有明确产出\n"
            "- 每个步骤只做一件事\n"
            '- 如果不需要工具，tool 填 "none"\n'
            "- 必须在最后输出 JSON 数组，不要加其他文字\n\n"
            "## 用户问题\n"
            f"{query}\n"
        )
        if extra:
            prompt += f"\n## 额外上下文\n{extra}\n"
        prompt += "\n## 计划 (JSON 数组)\n"
        return prompt

    @staticmethod
    def _parse_plan(raw: str) -> list[dict[str, Any]]:
        """从 LLM 输出中提取 JSON 计划数组。"""
        # 尝试直接解析
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

        # 尝试从 markdown 代码块中提取
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试匹配整个 JSON 数组
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # 兜底：返回一个"直接回答"的步骤
        logger.warning("PlanExecuteLoop: failed to parse plan JSON, fallback to single step")
        return [{"step": 1, "description": "直接回答用户问题", "tool": "none", "input": ""}]

    # ── Phase 2: Execute ──

    @staticmethod
    def _execute_step(
        agent: BaseAgent,
        step_item: dict[str, Any],
        total_tokens: dict[str, int],
        total_tool_calls: int,
        step_callback: callable | None,
    ) -> dict[str, Any]:
        """执行单个计划步骤。"""
        step_num = step_item.get("step", 0)
        description = step_item.get("description", "")
        tool_name = step_item.get("tool", "none")
        tool_input = step_item.get("input", "")

        result: dict[str, Any] = {
            "step": step_num,
            "description": description,
            "tool": tool_name,
            "tool_calls": 0,
            "output": "",
            "error": None,
        }

        # 无需工具 → LLM 直接回答
        if not tool_name or tool_name == "none":
            answer_prompt = f"根据以下问题完成第 {step_num} 步任务:\n任务: {description}\n\n请完成此步骤并给出结果。"
            msgs = [{"role": "system", "content": answer_prompt}]
            raw, usage = agent.llm_client.chat(msgs, temperature=0.5)
            PlanExecuteLoop._add_tokens(total_tokens, usage)
            result["output"] = raw.strip()
            return result

        # 需要工具 → 执行工具调用
        if step_callback:
            step_callback(
                {
                    "type": "think",
                    "step": step_num,
                    "thought": f"执行步骤 {step_num}: {description}",
                    "action": tool_name,
                    "action_input": {"input": str(tool_input)[:200]},
                }
            )

        # 执行工具（带重试）
        tool_args = PlanExecuteLoop._prepare_tool_args(tool_input)
        tool_result = PlanExecuteLoop._call_tool_safe(agent, tool_name, tool_args)
        result["tool_calls"] = 1

        if step_callback:
            step_callback(
                {
                    "type": "tool_result",
                    "step": step_num,
                    "tool": tool_name,
                    "result": str(tool_result.get("result", ""))[:300],
                    "elapsed_ms": tool_result.get("elapsed_ms", 0),
                }
            )

        if tool_result.get("success"):
            result["output"] = str(tool_result.get("result", ""))
        else:
            result["error"] = tool_result.get("error", "工具执行失败")
            result["output"] = f"[错误] {result['error']}"

        return result

    @staticmethod
    def _prepare_tool_args(tool_input: Any) -> dict[str, Any]:
        """将工具输入统一为 dict 格式。"""
        if tool_input is None:
            return {}
        if isinstance(tool_input, dict):
            return tool_input
        if isinstance(tool_input, str):
            # 尝试解析为 JSON
            try:
                return json.loads(tool_input)
            except (json.JSONDecodeError, TypeError):
                return {"query": tool_input}
        return {"query": str(tool_input)}

    @staticmethod
    def _call_tool_safe(agent: BaseAgent, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """安全调用工具（不抛异常）。"""
        try:
            from mcp_tools.registry import get_tool

            tool = get_tool(tool_name)
            if tool is None:
                return {"success": False, "error": f"未知工具: {tool_name}", "result": None, "elapsed_ms": 0}
            # 检查工具是否在 agent.tools 列表中
            if tool_name not in agent.tools:
                return {"success": False, "error": f"Agent 未授权工具: {tool_name}", "result": None, "elapsed_ms": 0}
            return tool.call(**args)
        except Exception as exc:
            return {"success": False, "error": str(exc), "result": None, "elapsed_ms": 0}

    # ── Phase 3: Review ──

    @staticmethod
    def _review(
        agent: BaseAgent,
        query: str,
        plan: list[dict[str, Any]],
        results: list[dict[str, Any]],
        total_tokens: dict[str, int],
    ) -> dict[str, Any]:
        """让 LLM 检查执行结果，判断是否完成。

        Returns:
            {"complete": bool, "answer": str, "missing": str, "status": str}
        """
        results_text = PlanExecuteLoop._format_executed_results(results)
        prompt = (
            "你是一个任务审查专家。检查以下执行结果，判断任务是否已完成。\n\n"
            f"## 用户原始问题\n{query}\n\n"
            f"## 原始计划\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            f"## 执行结果\n{results_text}\n\n"
            "## 输出格式\n"
            "请以 JSON 格式返回:\n"
            "{\n"
            '  "complete": true/false,\n'
            '  "answer": "如果 complete=true，给出综合后的最终答案",\n'
            '  "missing": "如果 complete=false，列出还缺少什么",\n'
            '  "status": "completed/partial/failed"\n'
            "}\n\n"
            "## 判断标准\n"
            "- 所有步骤都有有意义的结果 → complete=true\n"
            "- 有步骤失败或结果不足以回答问题 → complete=false\n"
            "- 步骤结果相互矛盾 → complete=false\n\n"
            "只输出 JSON，不加其他文字。"
        )

        msgs = [{"role": "system", "content": prompt}]
        raw, usage = agent.llm_client.chat(msgs, temperature=0.2)
        PlanExecuteLoop._add_tokens(total_tokens, usage)

        return PlanExecuteLoop._parse_review(raw)

    @staticmethod
    def _parse_review(raw: str) -> dict[str, Any]:
        """解析 Review 的 JSON 输出。"""
        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            pass

        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # 兜底：如果包含 "complete": true 字样，视为完成
        if '"complete": true' in raw.lower() or '"complete":true' in raw.lower():
            return {"complete": True, "answer": "任务已完成。", "missing": "", "status": "completed"}

        return {"complete": False, "answer": "", "missing": "无法解析 Review 结果", "status": "partial"}

    # ── 强制综合 ──

    @staticmethod
    def _force_synthesize(
        agent: BaseAgent,
        query: str,
        results: list[dict[str, Any]],
        total_tokens: dict[str, int],
    ) -> str:
        """循环耗尽后，强制综合所有结果生成最终答案。"""
        results_text = PlanExecuteLoop._format_executed_results(results)
        prompt = (
            "你是一个总结专家。基于以下步骤执行结果，综合给出最终答案。\n\n"
            f"## 用户问题\n{query}\n\n"
            f"## 已完成的步骤及结果\n{results_text}\n\n"
            "请综合所有信息，给出完整、准确的最终答案。"
        )

        msgs = [{"role": "system", "content": prompt}]
        raw, usage = agent.llm_client.chat(msgs, temperature=0.5)
        PlanExecuteLoop._add_tokens(total_tokens, usage)
        return raw.strip()

    # ── 辅助 ──

    @staticmethod
    def _format_executed_results(results: list[dict[str, Any]]) -> str:
        """格式化已执行步骤的结果为纯文本。"""
        lines = []
        for r in results:
            s = f"步骤 {r['step']}: {r['description']}\n"
            s += f"  工具: {r['tool']}\n"
            output = r.get("output", "")
            if r.get("error"):
                s += f"  错误: {r['error']}\n"
            if output:
                truncated = output[:500] + "..." if len(output) > 500 else output
                s += f"  结果: {truncated}\n"
            lines.append(s)
        return "\n".join(lines)

    @staticmethod
    def _add_tokens(total: dict[str, int], usage: Any) -> None:
        """累加 token 用量。"""
        if usage is None:
            return
        if hasattr(usage, "prompt_tokens"):
            total["prompt"] += usage.prompt_tokens
            total["completion"] += usage.completion_tokens
            total["total"] += usage.total_tokens
        elif isinstance(usage, dict):
            total["prompt"] += usage.get("prompt", 0)
            total["completion"] += usage.get("completion", 0)
            total["total"] += usage.get("total", 0)
