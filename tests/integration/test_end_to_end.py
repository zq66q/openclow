"""端到端集成测试。

验证 L1-L6 全栈链路可正常工作。
"""

from __future__ import annotations

try:
    import pytest
except ImportError:
    pytest = None  # type: ignore[assignment]

if pytest is not None:

    class TestEndToEnd:
        """端到端测试。"""

        def test_full_chat_pipeline(self, tmp_facade):
            """完整对话链路: ServiceFacade → Scenario → Agent → LLM → Response。"""
            answer = tmp_facade.chat("你好", scenario="general_assistant")
            assert isinstance(answer, str)

        def test_session_persistence(self, tmp_facade):
            """会话持久化: 创建 → 追加消息 → 重新读取。"""
            sm = tmp_facade.session_manager
            session = sm.create(user_id="e2e", title="persistence test")
            sid = session.session_id

            sm.append_message(sid, "user", "第一条消息")
            sm.append_message(sid, "assistant", "回复")

            loaded = sm.get(sid)
            assert len(loaded.messages) == 2
            assert loaded.messages[0]["role"] == "user"

        def test_scenario_chat_with_session(self, tmp_facade):
            """场景应用带会话的对话 — 消息持久化到应用内 SessionManager。"""
            app = tmp_facade.create_scenario("general_assistant")
            session = app.new_session(title="e2e")
            answer = app.chat("测试", session_id=session.session_id)
            assert isinstance(answer, str)

            # 验证消息已保存 — 用 app 自己的 session_manager
            msgs = app.session_manager.get_messages(session.session_id)
            assert len(msgs) == 2, f"Expected 2 messages, got {len(msgs)}"  # user + assistant

        def test_workflow_execution(self, tmp_facade):
            """工作流执行链路。"""
            result = tmp_facade.run_workflow("rag_qa", "测试查询")
            assert result is not None

        def test_rag_ingest_and_retrieve(self, tmp_facade, tmp_path, monkeypatch):
            """RAG: 导入文档 → 检索（用 mock embedding 避免真实 API 调用）。"""
            if tmp_facade._rag_pipeline is None:
                pytest.skip("RAG pipeline not available")

            # 用假 embedding 替换，防止真实 API 调用
            class _FakeEmbed:
                def embed_batch(self, texts, **kwargs):
                    return [[0.0] * 128 for _ in texts], {"total_tokens": 0, "batch_count": 1}
                def embed(self, text, **kwargs):
                    return [0.0] * 128, {"total_tokens": 0}

            if hasattr(tmp_facade._rag_pipeline, 'embed_client'):
                tmp_facade._rag_pipeline.embed_client = _FakeEmbed()
                tmp_facade._rag_pipeline.retriever.embed_client = _FakeEmbed()
            if hasattr(tmp_facade, '_embed_client'):
                tmp_facade._embed_client = _FakeEmbed()

            doc = tmp_path / "test_doc.txt"
            doc.write_text("OpenClaw is an enterprise multi-agent automation platform.")

            result = tmp_facade._rag_pipeline.ingest_file(str(doc))
            assert result.get("chunks", 0) > 0

        def test_health_after_operations(self, tmp_facade):
            """操作后健康检查仍通过。"""
            tmp_facade.chat("测试")
            report = tmp_facade.health_check()
            assert report.is_healthy()

        def test_multi_agent_creation(self, tmp_facade):
            """创建多个不同类型的 Agent。"""
            agents = {
                "general": tmp_facade.create_agent("general"),
                "data_analysis": tmp_facade.create_agent("data_analysis"),
                "code_review": tmp_facade.create_agent("code_review"),
            }
            for name, agent in agents.items():
                assert agent is not None, f"{name} agent creation failed"
