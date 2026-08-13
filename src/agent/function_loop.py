"""Function Calling 推理循环 — 基于 OpenAI tool_calls API（增强版 v2）。

增强（v2）:
  - 多个 tool_calls 并行执行（ThreadPoolExecutor）
  - 步骤超时保护
  - 工具执行重试
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from typing import TYPE_CHECKING, Any

from agent.base import AgentResult, AgentState, AgentStep
from core.logger import logger

if TYPE_CHECKING:
    from agent.base import BaseAgent


class FunctionCallingLoop:
    """OpenAI Function Calling 模式推理循环。

    配置项（通过 agent 属性）:
        max_steps:     最大推理步数
        step_timeout:  单步超时秒数（默认 60）
        tool_parallel: 是否并行执行工具（默认 True）
    """

    DEFAULT_STEP_TIMEOUT = 60

    @staticmethod
    def run(
        agent: BaseAgent,
        query: str,
        messages: list[dict[str, Any]] | None = None,
        image_data: str | None = None,
    ) -> AgentResult:
        start_time = time.perf_counter()
        steps: list[AgentStep] = []
        tool_calls_count = 0
        total_tokens: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}

        if messages is None:
            from agent.context_builder import ContextBuilder

            messages = ContextBuilder.build_messages(agent, query, image_data=image_data)

        tools_schema = FunctionCallingLoop._build_tools_schema(agent)
        step_timeout = getattr(agent, "step_timeout", FunctionCallingLoop.DEFAULT_STEP_TIMEOUT)
        tool_parallel = getattr(agent, "tool_parallel", True)

        agent.state = AgentState.RUNNING

        try:
            for step_num in range(1, agent.max_steps + 1):
                step_start = time.perf_counter()
                step = AgentStep(step_num=step_num)
                is_last_step = step_num == agent.max_steps

                # 最后一步强制不再传 tools，避免 LLM 继续调用工具而耗尽步数
                tools_for_this_step = tools_schema if (tools_schema and not is_last_step) else None

                # 1. 调用 LLM
                # 检测到图片且配置了独立 vision client → 切换 endpoint，否则用主 client
                if FunctionCallingLoop._has_image(messages) and agent.vision_llm_client:
                    client = agent.vision_llm_client
                    model = agent.vision_model
                else:
                    client = agent.llm_client
                    model = None
                response = client.chat_raw(
                    messages,
                    temperature=agent.temperature,
                    tools=tools_for_this_step,
                    model=model,
                )
                content = response["content"]
                tool_calls = response["tool_calls"]
                usage = response["usage"]
                if usage is not None:
                    total_tokens["prompt"] += usage.prompt_tokens
                    total_tokens["completion"] += usage.completion_tokens
                    total_tokens["total"] += usage.total_tokens

                logger.debug(
                    f"Agent [{agent.name}] FC step {step_num}",
                    extra={"has_content": bool(content), "tool_call_count": len(tool_calls or [])},
                )

                # 步超时检查
                step_elapsed = time.perf_counter() - step_start
                if step_timeout > 0 and step_elapsed > step_timeout:
                    logger.warning(f"Agent [{agent.name}] step {step_num} timeout")
                    agent.state = AgentState.DONE
                    return AgentResult(
                        answer="（处理超时）",
                        steps=steps,
                        tool_calls_count=tool_calls_count,
                        loop_type="function",
                        total_elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        token_usage=total_tokens,
                    )

                # 2. 无 tool_calls → 直接回答
                if not tool_calls:
                    step.thought = "直接回答"
                    step.elapsed_ms = round(step_elapsed * 1000, 2)
                    steps.append(step)
                    agent.state = AgentState.DONE
                    return AgentResult(
                        answer=content.strip(),
                        steps=steps,
                        tool_calls_count=tool_calls_count,
                        loop_type="function",
                        total_elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        token_usage=total_tokens,
                    )

                # 3. 执行工具（串行或并行）
                tool_results: list[dict[str, Any]] = []

                if tool_parallel and len(tool_calls) > 1:
                    # 并行执行
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tool_calls)) as executor:
                        futures: dict[concurrent.futures.Future, int] = {}
                        for i, tc in enumerate(tool_calls):
                            try:
                                args = json.loads(tc["arguments"])
                            except json.JSONDecodeError:
                                args = {"raw": tc["arguments"]}
                            future = executor.submit(
                                FunctionCallingLoop._execute_tool,
                                agent,
                                tc["name"],
                                args,
                            )
                            futures[future] = i

                        results_map: dict[int, str] = {}
                        for future in concurrent.futures.as_completed(futures):
                            idx = futures[future]
                            try:
                                results_map[idx] = future.result(timeout=30)
                            except Exception as exc:
                                results_map[idx] = f"工具执行异常: {exc}"

                        for i, tc in enumerate(tool_calls):
                            observation = results_map.get(i, "（执行超时��")
                            tool_results.append(
                                {
                                    "tool_call_id": tc["id"],
                                    "role": "tool",
                                    "content": observation,
                                }
                            )
                            tool_calls_count += 1

                        # 记录到 step
                        step.action = ", ".join(tc["name"] for tc in tool_calls)
                        step.observation = "; ".join(results_map.values())
                else:
                    # 串行执行
                    for tc in tool_calls:
                        try:
                            args = json.loads(tc["arguments"])
                        except json.JSONDecodeError:
                            args = {"raw": tc["arguments"]}

                        observation = FunctionCallingLoop._execute_tool(agent, tc["name"], args)
                        tool_results.append(
                            {
                                "tool_call_id": tc["id"],
                                "role": "tool",
                                "content": observation,
                            }
                        )
                        tool_calls_count += 1
                        step.action = tc["name"]
                        step.observation = observation

                step.elapsed_ms = round((time.perf_counter() - step_start) * 1000, 2)
                steps.append(step)

                # 4. 追加到 messages
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in tool_calls
                    ],
                }
                messages.append(assistant_msg)
                messages.extend(tool_results)

            # 达到最大步数
            agent.state = AgentState.DONE
            return AgentResult(
                answer="（已达最大推理步数，未能得出最终答案）",
                steps=steps,
                tool_calls_count=tool_calls_count,
                loop_type="function",
                total_elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                token_usage=total_tokens,
            )

        except Exception as exc:
            agent.state = AgentState.ERROR
            logger.error(f"Agent [{agent.name}] FC loop error", extra={"error": str(exc)})
            return AgentResult(
                error=str(exc),
                steps=steps,
                tool_calls_count=tool_calls_count,
                loop_type="function",
                total_elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                token_usage=total_tokens,
            )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tools_schema(agent: BaseAgent) -> list[dict[str, Any]]:
        if not agent.tools:
            return []
        from mcp_tools.registry import get_registry

        registry = get_registry()
        schemas: list[dict[str, Any]] = []
        for tname in agent.tools:
            tool = registry.get(tname)
            if tool:
                schemas.append(tool.to_openai_schema())
        return schemas

    @staticmethod
    def _execute_tool(agent: BaseAgent, tool_name: str, args: dict) -> str:
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
                # 截断过长的返回结果，防止上下文膨胀
                text = str(result["result"])
                if len(text) > 2000:
                    text = text[:2000] + "\n...(结果已截断)"
                return text
            return f"工具执行失败: {result['error']}"
        except Exception as exc:
            logger.error(f"Agent [{agent.name}] tool error", extra={"tool": tool_name, "error": str(exc)})
            return f"工具执行异常: {exc}"

    # ------------------------------------------------------------------
    # 视觉模型检测（多模态图片切换 client + model）
    # ------------------------------------------------------------------

    @staticmethod
    def _has_image(messages: list[dict[str, Any]]) -> bool:
        """检测消息列表中是否包含图片（多模态 content 数组）。"""
        return any(isinstance(msg.get("content"), list) for msg in messages)

    @staticmethod
    def _resolve_model(agent: BaseAgent, messages: list[dict[str, Any]]) -> str | None:
        """根据消息内容决定使用的模型。有图片时返回视觉模型名。"""
        if FunctionCallingLoop._has_image(messages) and agent.vision_model:
            return agent.vision_model
        return None
