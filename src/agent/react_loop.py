"""ReAct 推理循环 — Thought -> Action -> Observation -> Final Answer（增强版 v2）。

增强（v2）:
  - 步骤级超时保护（防止单步卡死）
  - 工具执行重试
  - 结构化 JSON 解析回退（当 LLM 不遵循 ReAct 格式时）
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from agent.base import AgentResult, AgentState, AgentStep
from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent

# 解析 ReAct 格式的正则
_RE_THOUGHT = re.compile(r"Thought:\s*(.+?)(?:\n|$)", re.IGNORECASE)
_RE_ACTION = re.compile(r"Action:\s*(\S+)(?:\n|$)", re.IGNORECASE)
_RE_ACTION_INPUT = re.compile(r"Action Input:\s*(.+?)(?:\n|$)", re.IGNORECASE)
_RE_FINAL_ANSWER = re.compile(r"Final Answer:\s*(.+)", re.IGNORECASE | re.DOTALL)


class ReActLoop:
    """ReAct 模式推理循环。

    配置项（通过 agent 属性设置）:
        max_steps:    最大推理步数（默认 10）
        step_timeout: 单步超时秒数（默认 60，0=无超时）
        tool_retry:   工具执行重试次数（默认 1）
    """

    # 默认配置
    DEFAULT_STEP_TIMEOUT = 60
    DEFAULT_TOOL_RETRY = 1

    @staticmethod
    def run(
        agent: BaseAgent,
        query: str,
        messages: list[dict[str, Any]] | None = None,
        step_callback: callable | None = None,
        image_data: str | None = None,
    ) -> AgentResult:
        start_time = time.perf_counter()
        steps: list[AgentStep] = []
        tool_calls_count = 0
        total_tokens: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}

        if messages is None:
            from agent.context_builder import ContextBuilder
            messages = ContextBuilder.build_messages(agent, query, image_data=image_data)

        step_timeout = getattr(agent, "step_timeout", ReActLoop.DEFAULT_STEP_TIMEOUT)
        tool_retry = getattr(agent, "tool_retry", ReActLoop.DEFAULT_TOOL_RETRY)

        agent.state = AgentState.RUNNING

        try:
            for step_num in range(1, agent.max_steps + 1):
                step_start = time.perf_counter()
                step = AgentStep(step_num=step_num)

                # 1. 调用 LLM（步超时检测）
                # 检测到图片且配置了独立 vision client → 切换 endpoint，否则用主 client
                if ReActLoop._has_image(messages) and agent.vision_llm_client:
                    client = agent.vision_llm_client
                    model = agent.vision_model
                else:
                    client = agent.llm_client
                    model = None
                raw_output, usage = client.chat(
                    messages, model=model, temperature=agent.temperature
                )
                if usage is not None:
                    total_tokens["prompt"] += getattr(usage, "prompt_tokens", 0)
                    total_tokens["completion"] += getattr(usage, "completion_tokens", 0)
                    total_tokens["total"] += getattr(usage, "total_tokens", 0)

                logger.debug(
                    f"Agent [{agent.name}] ReAct step {step_num}",
                    extra={"output": raw_output[:300]},
                )

                # 步超时检查
                step_elapsed = time.perf_counter() - step_start
                if step_timeout > 0 and step_elapsed > step_timeout:
                    logger.warning(f"Agent [{agent.name}] step {step_num} timeout ({step_elapsed:.1f}s)")
                    raw_output = "Thought: 步骤超时，将根据已有信息回答。\nFinal Answer: 处理超时，这是目前已知的信息。"

                # 2. 检查 Final Answer
                final_match = _RE_FINAL_ANSWER.search(raw_output)
                if final_match:
                    step.thought = "得出最终答案"
                    step.elapsed_ms = round(step_elapsed * 1000, 2)
                    steps.append(step)
                    agent.state = AgentState.DONE
                    final_answer_text = final_match.group(1).strip()
                    if step_callback:
                        step_callback({"type": "final", "answer": final_answer_text})
                    return AgentResult(
                        answer=final_answer_text,
                        steps=steps, tool_calls_count=tool_calls_count,
                        loop_type="react",
                        total_elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        token_usage=total_tokens,
                    )

                # 3. 解析 Thought / Action / Action Input
                thought = _RE_THOUGHT.search(raw_output)
                action = _RE_ACTION.search(raw_output)
                action_input = _RE_ACTION_INPUT.search(raw_output)

                # 结构化回退：如果 ReAct 格式不匹配，尝试 JSON 解析
                if not action:
                    parsed = ReActLoop._try_json_parse(raw_output)
                    if parsed:
                        action, action_input = parsed

                if action:
                    step.thought = thought.group(1).strip() if thought else ""
                    step.action = action.group(1).strip() if isinstance(action, re.Match) else action

                    # 回调：通知外层"开始思考+调用工具"
                    if step_callback:
                        step_callback({"type": "think", "step": step_num, "thought": step.thought,
                                       "action": step.action, "action_input": None})

                    if isinstance(action_input, re.Match):
                        try:
                            step.action_input = json.loads(action_input.group(1).strip())
                        except json.JSONDecodeError:
                            step.action_input = {"raw": action_input.group(1).strip()}
                    else:
                        step.action_input = action_input or {}

                    # 回调：通知外层"委派任务"
                    if step_callback:
                        step_callback({"type": "delegate", "step": step_num,
                                       "tool": step.action,
                                       "task": str(step.action_input.get("task", step.action_input))[:300]})

                    # 4. 执行工具（带重试）
                    observation = ReActLoop._execute_tool_with_retry(
                        agent, step.action, step.action_input, max_retries=tool_retry
                    )
                    step.observation = observation
                    tool_calls_count += 1

                    # 回调：通知外层"收到工具结果"
                    if step_callback:
                        step_callback({"type": "tool_result", "step": step_num,
                                       "tool": step.action,
                                       "result": observation[:500],
                                       "elapsed_ms": round(step_elapsed * 1000, 2)})

                    messages.append({"role": "assistant", "content": raw_output})
                    messages.append({"role": "user", "content": f"Observation: {observation}"})
                else:
                    # 无 Action 也无 Final Answer → 直接当作回答
                    step.thought = "直接回答"
                    step.elapsed_ms = round(step_elapsed * 1000, 2)
                    steps.append(step)
                    agent.state = AgentState.DONE
                    return AgentResult(
                        answer=raw_output.strip(),
                        steps=steps, tool_calls_count=tool_calls_count,
                        loop_type="react",
                        total_elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        token_usage=total_tokens,
                    )

                step.elapsed_ms = round(step_elapsed * 1000, 2)
                steps.append(step)

            # 达到最大步数
            agent.state = AgentState.DONE
            return AgentResult(
                answer="（已达最大推理步数，未能得出最终答案）",
                steps=steps, tool_calls_count=tool_calls_count,
                loop_type="react",
                total_elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                token_usage=total_tokens,
            )

        except Exception as exc:
            agent.state = AgentState.ERROR
            logger.error(f"Agent [{agent.name}] ReAct loop error", extra={"error": str(exc)})
            return AgentResult(
                error=str(exc), steps=steps, tool_calls_count=tool_calls_count,
                loop_type="react",
                total_elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                token_usage=total_tokens,
            )

    # ------------------------------------------------------------------
    # 工具执行（带重试）
    # ------------------------------------------------------------------

    @staticmethod
    def _execute_tool_with_retry(
        agent: BaseAgent, tool_name: str, args: dict, max_retries: int = 1
    ) -> str:
        """带重试的工具执行。"""
        last_err = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.warning(
                    f"Agent [{agent.name}] tool [{tool_name}] retry {attempt}/{max_retries}"
                )
                time.sleep(0.5)

            result = ReActLoop._execute_tool(agent, tool_name, args)
            if not result.startswith("错误") and not result.startswith("工具执行"):
                return result
            last_err = result

        return last_err

    @staticmethod
    def _execute_tool(agent: BaseAgent, tool_name: str, args: dict) -> str:
        """安全执行工具。"""
        if tool_name not in agent.tools:
            return f"错误：工具 '{tool_name}' 不在 Agent [{agent.name}] 的允许列表中"

        from mcp_tools.registry import get_tool

        tool_instance = get_tool(tool_name)
        if tool_instance is None:
            return f"错误：工具 '{tool_name}' 未注册"

        logger.info(f"Agent [{agent.name}] executing tool", extra={"tool": tool_name, "args": args})

        try:
            result = tool_instance.call(**args)
            if result["success"]:
                return str(result["result"])
            return f"工具执行失败: {result['error']}"
        except Exception as exc:
            logger.error(f"Agent [{agent.name}] tool error", extra={"tool": tool_name, "error": str(exc)})
            return f"工具执行异常: {exc}"

    # ------------------------------------------------------------------
    # JSON 解析回退
    # ------------------------------------------------------------------

    @staticmethod
    def _try_json_parse(raw_output: str) -> tuple | None:
        """尝试从 LLM 输出中解析 JSON 格式的工具调用。

        支持格式:
            {"tool": "calculator", "args": {"expression": "1+1"}}
            {"action": "calculator", "action_input": {"expression": "1+1"}}
        """
        try:
            # 尝试直接解析整个输出
            data = json.loads(raw_output.strip())
            if "tool" in data or "action" in data:
                tool = data.get("tool") or data.get("action")
                args = data.get("args") or data.get("action_input") or data.get("parameters", {})
                if isinstance(tool, str):
                    return (tool, args)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 块
        json_match = re.search(r"\{[^{}]*\}", raw_output)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                if "tool" in data or "action" in data:
                    tool = data.get("tool") or data.get("action")
                    args = data.get("args") or data.get("action_input") or data.get("parameters", {})
                    if isinstance(tool, str):
                        return (tool, args)
            except json.JSONDecodeError:
                pass

        return None

    # ------------------------------------------------------------------
    # 视觉模型检测（多模态图片切换 client + model）
    # ------------------------------------------------------------------

    @staticmethod
    def _has_image(messages: list[dict[str, Any]]) -> bool:
        """检测消息列表中是否包含图片（多模态 content 数组）。"""
        for msg in messages:
            if isinstance(msg.get("content"), list):
                return True
        return False

    @staticmethod
    def _resolve_model(agent: BaseAgent, messages: list[dict[str, Any]]) -> str | None:
        """根据消息内容决定使用的模型。有图片时返回视觉模型名。"""
        if ReActLoop._has_image(messages) and agent.vision_model:
            return agent.vision_model
        return None
