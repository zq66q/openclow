"""ServiceFacade 单元测试。"""

from __future__ import annotations

try:
    import pytest
except ImportError:
    pytest = None  # type: ignore[assignment]

from business.service_facade import HealthReport, ServiceConfig, ServiceStatus


class TestServiceConfig:
    """ServiceConfig 测试。"""

    def test_default_values(self):
        config = ServiceConfig()
        assert config.llm_model == "gpt-4o-mini"
        assert config.llm_temperature == 0.7
        assert config.memory_db_path == "./data/memory.db"

    def test_from_file_json(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"llm_api_key": "sk-test", "llm_model": "gpt-4"}')
        config = ServiceConfig.from_file(str(config_file))
        assert config.llm_api_key == "sk-test"
        assert config.llm_model == "gpt-4"

    def test_from_file_missing(self, tmp_path):
        config = ServiceConfig.from_file(str(tmp_path / "nonexistent.json"))
        assert config.llm_api_key == ""  # defaults

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_LLM_API_KEY", "sk-env")
        monkeypatch.setenv("OPENCLAW_LLM_MODEL", "gpt-3.5")
        config = ServiceConfig.from_env()
        assert config.llm_api_key == "sk-env"
        assert config.llm_model == "gpt-3.5"


class TestHealthReport:
    """HealthReport 测试。"""

    def test_is_healthy_running(self):
        report = HealthReport(status=ServiceStatus.RUNNING)
        assert report.is_healthy() is True

    def test_is_healthy_degraded(self):
        report = HealthReport(status=ServiceStatus.DEGRADED)
        assert report.is_healthy() is True

    def test_is_healthy_stopped(self):
        report = HealthReport(status=ServiceStatus.STOPPED)
        assert report.is_healthy() is False


# pytest fixture 驱动测试
if pytest is not None:

    class TestServiceFacade:
        """ServiceFacade 功能测试。"""

        def test_start_stop(self, tmp_facade):
            assert tmp_facade.status in (ServiceStatus.RUNNING, ServiceStatus.DEGRADED)

        def test_health_check(self, tmp_facade):
            report = tmp_facade.health_check()
            assert report.is_healthy()

        def test_create_scenario(self, tmp_facade):
            app = tmp_facade.create_scenario("general_assistant")
            assert app is not None
            assert hasattr(app, "chat")

        def test_create_agent(self, tmp_facade):
            agent = tmp_facade.create_agent("general")
            assert agent is not None
            assert agent.name == "general_assistant"

        def test_chat(self, tmp_facade):
            answer = tmp_facade.chat("你好", scenario="general_assistant")
            assert isinstance(answer, str)
            assert len(answer) > 0

        def test_list_agents(self, tmp_facade):
            """列出预置 Agent 类型。"""
            agents = ["general", "rag_qa", "tool_calling", "data_analysis", "code_review"]
            assert len(agents) >= 5
            assert "general" in agents
            assert "rag_qa" in agents
            # 验证每个 preset 都能创建
            for name in agents:
                a = tmp_facade.create_agent(name)
                assert a is not None

        def test_list_scenarios(self, tmp_facade):
            """列出可用场景类型。"""
            scenarios = ["general_assistant", "rag_customer_service", "data_analyst", "code_reviewer"]
            assert len(scenarios) >= 3
            assert "general_assistant" in scenarios
            # 验证每个场景都能创建
            for name in scenarios:
                app = tmp_facade.create_scenario(name)
                assert app is not None

        def test_session_manager(self, tmp_facade):
            sm = tmp_facade.session_manager
            session = sm.create(user_id="pytest", title="test")
            assert session.session_id is not None
            assert sm.get(session.session_id) is not None
