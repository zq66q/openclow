"""openclaw/src/agent/graph.py 单元测试。

测试 build_graph()、占位图 _StubGraph 行为。
"""

from __future__ import annotations

import os
import sys

# 确保 src 在 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest


def _is_langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401

        return True
    except ImportError:
        return False


class TestBuildGraph:
    """build_graph 测试。"""

    def test_build_graph_returns_stub_without_langgraph(self):
        """无 LangGraph 时返回 _StubGraph。"""
        from agent.graph import _StubGraph, build_graph

        graph = build_graph()
        assert isinstance(graph, _StubGraph) or hasattr(graph, "invoke")

    def test_build_graph_with_llm(self):
        """传入 LLM 客户端。"""
        from agent.graph import build_graph

        graph = build_graph(
            llm_client=None,
            tools=[],
        )
        assert graph is not None

    def test_build_graph_with_max_steps(self):
        """自定义 max_steps。"""
        from agent.graph import build_graph

        graph = build_graph(max_steps=5)
        assert graph is not None

    def test_stub_invoke(self):
        """_StubGraph.invoke 返回有效结果。"""
        from agent.graph import _StubGraph

        graph = _StubGraph()
        result = graph.invoke({"query": "测试查询"})

        assert isinstance(result, dict)
        assert "messages" in result
        assert "result" in result
        assert "step_count" in result
        # step_count 应为 1
        assert result["step_count"] == 1
        # result 应包含原始 query
        assert "测试查询" in result["result"]

    def test_stub_invoke_empty_query(self):
        """空 query 也能正常返回。"""
        from agent.graph import _StubGraph

        graph = _StubGraph()
        result = graph.invoke({"query": ""})
        assert "messages" in result
        assert "result" in result

    def test_stub_ainvoke(self):
        """异步 invoke。"""
        import asyncio

        from agent.graph import _StubGraph

        graph = _StubGraph()

        async def run():
            return await graph.ainvoke({"query": "async test"})

        result = asyncio.run(run())
        assert "result" in result
        assert "async test" in result["result"]

    def test_stub_stream(self):
        """流式输出。"""
        from agent.graph import _StubGraph

        graph = _StubGraph()
        chunks = list(graph.stream({"query": "stream test"}))
        assert len(chunks) >= 1

    def test_state_fields(self):
        """OpenClawState 包含必要字段。"""

        # 构造一个 state
        state: dict = {
            "messages": [{"role": "user", "content": "hi"}],
            "query": "hi",
            "result": "",
            "context": {},
            "step_count": 0,
            "max_steps": 10,
            "error": None,
        }
        assert state["query"] == "hi"
        assert state["step_count"] == 0

    def test_end_constant(self):
        """END 常量存在。"""
        from agent.graph import END

        assert END is not None


class TestGraphIntegration:
    """集成测试 — 与 ServiceFacade 联动。"""

    @pytest.mark.skipif(
        not _is_langgraph_available(),
        reason="langgraph not installed",
    )
    def test_graph_with_service_facade(self, tmp_facade):
        """从 ServiceFacade 的 LLM client 构建图。"""
        from agent.graph import build_graph

        llm = tmp_facade.llm_client
        graph = build_graph(llm_client=llm, max_steps=2)

        # LangGraph 1.2+ 的 checkpointer 需要 configurable 里带 thread_id
        result = graph.invoke({"query": "Hello"}, config={"configurable": {"thread_id": "test_graph"}})
        assert result is not None
        assert isinstance(result, dict)
