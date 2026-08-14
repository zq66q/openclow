"""pytest 全局配置和 fixtures。

提供:
    - tmp_facade: 临时 ServiceFacade 实例
    - tmp_session: 临时会话
    - mock_llm: Mock LLM 客户端
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from contextlib import suppress
from typing import Any

try:
    import pytest
except ImportError:
    pytest = None  # type: ignore[assignment]


# ── fixtures ──

if pytest is not None:

    @pytest.fixture(scope="session")
    def test_db_dir() -> str:
        """全局临时数据目录。"""
        d = tempfile.mkdtemp(prefix="openclaw_test_")
        yield d
        import shutil

        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def mock_llm() -> Any:
        """Mock LLM 客户端 — 无需 API Key。"""
        from core.llm_client import BaseLLMClient

        class MockLLM(BaseLLMClient):
            name = "mock"

            def __init__(self) -> None:
                super().__init__(api_key="mock", base_url="http://mock", default_model="mock")

            def chat(self, messages, **kwargs):
                last = messages[-1]["content"] if messages else ""
                return f"[MockLLM] {last}", None

            def embed(self, texts, **kwargs):
                return [[0.0] * 128 for _ in texts], None

        return MockLLM()

    @pytest.fixture
    def tmp_facade(test_db_dir: str) -> Generator[Any, None, None]:
        """临时 ServiceFacade 实例。"""
        from business.service_facade import ServiceConfig, ServiceFacade

        config = ServiceConfig(
            llm_api_key="mock",
            llm_base_url="http://mock",
            llm_model="mock",
            memory_db_path=os.path.join(test_db_dir, "memory.db"),
            rag_persist_dir=os.path.join(test_db_dir, "rag"),
            state_db_path=os.path.join(test_db_dir, "state.db"),
        )
        facade = ServiceFacade(config)
        facade.start()
        yield facade
        facade.shutdown()

    @pytest.fixture
    def tmp_session(tmp_facade: Any) -> Any:
        """临时会话。"""
        sm = tmp_facade.session_manager
        return sm.create(user_id="test", title="pytest session")

    @pytest.fixture(autouse=True)
    def _patch_env(monkeypatch: Any) -> None:
        """自动清理环境变量，避免测试互相污染。

        注意：core.settings 的 load_dotenv(override=True) 只在 import 时执行一次，
        会用 .env 强制覆盖已有变量。因此先触发该 import，再设置测试用值，
        否则 .env 中的 OPENCLAW_AUTH_MODE 等会污染所有测试。
        """
        with suppress(Exception):
            import core.settings  # noqa: F401
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENCLAW_LLM_API_KEY", raising=False)
        monkeypatch.setenv("OPENCLAW_AUTH_MODE", "none")


# ── 独立辅助函数（无 pytest 时也可用） ──


def create_mock_llm() -> Any:
    """创建 Mock LLM（独立函数，不依赖 pytest）。"""
    from core.llm_client import BaseLLMClient

    class MockLLM(BaseLLMClient):
        name = "mock"

        def __init__(self) -> None:
            super().__init__(api_key="mock", base_url="http://mock", default_model="mock")

        def chat(self, messages, **kwargs):
            last = messages[-1]["content"] if messages else ""
            return f"[MockLLM] {last}", None

        def embed(self, texts, **kwargs):
            return [[0.0] * 128 for _ in texts], None

    return MockLLM()
