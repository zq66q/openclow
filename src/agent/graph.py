"""LangGraph 图定义 — 将 ReAct 循环编排为可持久化的状态图。

提供 langgraph.json 入口函数 build_graph()，
支持状态持久化、checkpointer、streaming。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, Sequence

from core.logger import logger

# ── TypedDict 状态（兼容 LangGraph） ──

try:
    from typing import NotRequired, Required
except ImportError:
    from typing_extensions import NotRequired, Required  # type: ignore[assignment]

# 尽量兼容 LangGraph 存在 / 不存在的环境
_HAS_LANGGRAPH = False
try:
    from langgraph.graph import END, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.checkpoint import BaseCheckpointSaver
    _HAS_LANGGRAPH = True
except ImportError:
    # LangGraph 未安装 — 提供最小 TypedDict + 占位函数
    class END:  # type: ignore[no-redef]
        pass

    class StateGraph:  # type: ignore[no-redef]
        pass

    def add_messages(left: Any, right: Any) -> Any:
        return []

    BaseCheckpointSaver = None  # type: ignore[misc,assignment]


# Python 3.12+ TypedDict 用 ReadOnly / NotRequired
# 兼容不同版本
try:
    from typing import TypedDict  # Python 3.12+
except ImportError:
    from typing_extensions import TypedDict

if hasattr(TypedDict, '__flags__'):
    # Python 3.12+ — 用原生语法
    class OpenClawState(TypedDict, total=False):  # type: ignore[valid-type]
        messages: Annotated[list[dict[str, Any]], add_messages]
        query: str
        result: str
        context: dict[str, Any]
        step_count: int
        max_steps: int
        error: str | None
else:
    # Python 3.10 / 3.11 — 普通 TypedDict
    class OpenClawState(TypedDict, total=False):  # type: ignore[no-redef]
        messages: Any  # langgraph 会用 Annotated reducer
        query: str
        result: str
        context: dict[str, Any]
        step_count: int
        max_steps: int
        error: str | None


# ── 图构建 ──


def build_graph(
    llm_client: Any = None,
    tools: list[Any] | None = None,
    checkpointer: Any = None,
    max_steps: int = 10,
) -> Any:
    """构建 LangGraph ReAct 状态图。

    Args:
        llm_client: LLM 客户端实例（BaseLLMClient 兼容）
        tools: 可用工具列表
        checkpointer: LangGraph checkpoint（None 则使用 MemorySaver）
        max_steps: 最大推理步数

    Returns:
        Compiled LangGraph StateGraph
    """
    if not _HAS_LANGGRAPH:
        logger.warning("LangGraph not installed — returning stub graph")
        return _StubGraph()

    if tools is None:
        tools = []

    # 注入默认 LLM 客户端
    _llm = llm_client
    _tools = tools
    _max_steps = max_steps

    # ── 节点 ──

    def agent_node(state: dict[str, Any]) -> dict[str, Any]:
        """Agent 推理节点 — 调用 LLM，可能产生工具调用。"""
        messages = state.get("messages", [])
        query = state.get("query", "")
        step = state.get("step_count", 0)

        if _llm is None:
            return {
                "messages": [{"role": "assistant", "content": "[No LLM configured]"}],
                "result": "[No LLM configured]",
                "step_count": step + 1,
            }

        # 首次调用时注入 query
        if step == 0 and query:
            messages = [{"role": "user", "content": query}]

        try:
            result_text, _ = _llm.chat(messages, tools=_tools)
        except Exception as exc:
            logger.error(f"Agent node error: {exc}")
            return {
                "messages": messages + [{"role": "assistant", "content": f"Error: {exc}"}],
                "result": f"Error: {exc}",
                "error": str(exc),
                "step_count": step + 1,
            }

        return {
            "messages": messages + [{"role": "assistant", "content": result_text}],
            "result": result_text,
            "step_count": step + 1,
        }

    def tool_node(state: dict[str, Any]) -> dict[str, Any]:
        """工具执行节点。"""
        messages = state.get("messages", [])
        step = state.get("step_count", 0)

        # 提取最后一个助手消息中的工具调用
        tool_results: list[dict[str, Any]] = []
        last_msg = messages[-1] if messages else {}
        tool_calls = last_msg.get("tool_calls", []) if isinstance(last_msg, dict) else getattr(last_msg, "tool_calls", [])

        if not tool_calls:
            return {"messages": messages, "step_count": step}

        for tc in tool_calls:
            tool_name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            tool_args = tc.get("arguments", {}) if isinstance(tc, dict) else getattr(tc, "arguments", {})
            tool_result = f"[Tool {tool_name} executed with {tool_args}]"
            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", ""),
                "content": tool_result,
            })

        return {
            "messages": messages + tool_results,
            "step_count": step,
        }

    def should_continue(state: dict[str, Any]) -> str:
        """路由：决定下一步是继续推理还是结束。"""
        step = state.get("step_count", 0)
        error = state.get("error")

        if error:
            return "end"
        if step >= _max_steps:
            return "end"

        # 检查最后一条消息是否有 tool_calls
        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            tc = last_msg.get("tool_calls", []) if isinstance(last_msg, dict) else getattr(last_msg, "tool_calls", [])
            if tc:
                return "tools"
        return "end"

    # ── 构图 ──
    graph = StateGraph(OpenClawState)

    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "end": END},
    )
    graph.add_edge("tools", "agent")

    # checkpoint
    if checkpointer is None:
        try:
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
        except ImportError:
            pass

    return graph.compile(checkpointer=checkpointer)


# ── 占位图（LangGraph 未安装时） ──


class _StubGraph:
    """LangGraph 不可用时的占位。"""

    def invoke(self, state: dict[str, Any], config: dict | None = None) -> dict[str, Any]:
        query = state.get("query", "")
        return {
            "messages": [{"role": "assistant", "content": f"[StubGraph] Received: {query}"}],
            "result": f"[StubGraph] No LLM response for: {query}",
            "step_count": 1,
        }

    async def ainvoke(self, state: dict[str, Any], config: dict | None = None) -> dict[str, Any]:
        return self.invoke(state, config)

    def stream(self, state: dict[str, Any], config: dict | None = None) -> Any:
        yield {"agent": {"result": f"[StubGraph stream] {state.get('query', '')}"}}
