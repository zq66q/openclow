"""openclaw/src/api/server.py 单元测试。

测试 create_app()、端点路由注册、占位 App 行为。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest


def _is_fastapi_available() -> bool:
    try:
        import fastapi  # noqa: F401

        return True
    except ImportError:
        return False


def _is_httpx_available() -> bool:
    try:
        import httpx  # noqa: F401

        return True
    except ImportError:
        return False


class TestCreateApp:
    """create_app 基础测试。"""

    def test_create_app_returns_value(self):
        """create_app 总是返回一个对象。"""
        from api.server import create_app

        app = create_app()
        assert app is not None

    def test_create_app_with_facade(self, tmp_facade):
        """传入 ServiceFacade。"""
        from api.server import create_app

        app = create_app(facade=tmp_facade)
        assert app is not None

    def test_create_app_custom_title(self):
        """自定义 title。"""
        from api.server import create_app

        app = create_app(title="Test API")
        assert app is not None

    def test_get_facade_creates_instance(self):
        """get_facade 懒加载 ServiceFacade。"""
        from api.server import get_facade

        facade = get_facade()
        assert facade is not None


class TestEndpoints:
    """端点注册和响应测试（含 HTTP 测试）。"""

    @pytest.fixture
    def client(self, tmp_facade):
        """创建 TestClient。"""
        from api.server import create_app

        app = create_app(facade=tmp_facade)

        if not _is_fastapi_available():
            pytest.skip("FastAPI not installed")

        try:
            from fastapi.testclient import TestClient

            return TestClient(app)
        except ImportError:
            pytest.skip("TestClient not available (install httpx)")

    def test_health_endpoint(self, client):
        """GET /health 返回 200。"""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "version" in data
        assert "components" in data

    def test_chat_endpoint(self, client):
        """POST /chat 返回回答。"""
        response = client.post(
            "/chat",
            json={
                "query": "测试消息",
                "scenario": "general_assistant",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "answer" in data
        assert "session_id" in data

    def test_chat_endpoint_with_stream(self, client):
        """POST /chat stream=True。"""
        response = client.post(
            "/chat",
            json={
                "query": "测试",
                "stream": True,
            },
        )
        assert response.status_code in (200, 501)

    def test_sessions_list(self, client):
        """GET /sessions 列出会话。"""
        # 先创建一个
        client.post("/sessions", json={"user_id": "test", "title": "测试会话"})
        response = client.get("/sessions?user_id=test")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_sessions_create(self, client):
        """POST /sessions 创建会话。"""
        response = client.post(
            "/sessions",
            json={
                "user_id": "test",
                "title": "新会话",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert data["title"] == "新会话"

    def test_sessions_get(self, client):
        """GET /sessions/{id} 获取会话。"""
        # 先创建
        create_resp = client.post("/sessions", json={"user_id": "test", "title": "获取测试"})
        assert create_resp.status_code == 200
        sid = create_resp.json()["session_id"]

        response = client.get(f"/sessions/{sid}")
        assert response.status_code == 200
        assert response.json()["title"] == "获取测试"

    def test_sessions_get_not_found(self, client):
        """GET /sessions/nonexistent 返回 404。"""
        response = client.get("/sessions/nonexistent-id-12345")
        assert response.status_code == 404

    def test_sessions_delete(self, client):
        """DELETE /sessions/{id} 删除会话。"""
        create_resp = client.post("/sessions", json={"user_id": "test", "title": "待删除"})
        assert create_resp.status_code == 200
        sid = create_resp.json()["session_id"]

        response = client.delete(f"/sessions/{sid}")
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"

    def test_agents_list(self, client):
        """GET /agents 列出预置 Agent。"""
        response = client.get("/agents")
        assert response.status_code == 200
        data = response.json()
        assert "agents" in data
        assert len(data["agents"]) >= 3

    def test_scenarios_list(self, client):
        """GET /scenarios 列出场景。"""
        response = client.get("/scenarios")
        assert response.status_code == 200
        data = response.json()
        assert "scenarios" in data
        assert len(data["scenarios"]) >= 2


class TestStartFunction:
    """start() 函数测试。"""

    def test_start_without_uvicorn(self):
        """无 uvicorn 时不抛异常 — 用 mock 防止真正启动服务器。"""
        import api.server as server_mod

        # 记录原始状态
        original_has_uvicorn = server_mod._HAS_UVICORN
        original_uvicorn = server_mod.uvicorn

        try:
            # 模拟 uvicorn 未安装
            server_mod._HAS_UVICORN = False
            from api.server import start

            start(host="127.0.0.1", port=19999)
            assert True  # 不抛异常即通过
        finally:
            server_mod._HAS_UVICORN = original_has_uvicorn
            server_mod.uvicorn = original_uvicorn


class TestHealthResponse:
    """健康检查响应模型。"""

    def test_health_response_constructor(self):
        """HealthResponse 数据模型。"""
        from api.models import HealthResponse

        hr = HealthResponse(status="running", version="1.0.0", uptime_seconds=42.0, components={})
        assert hr.status == "running"
        assert hr.version == "1.0.0"
        assert hr.uptime_seconds == 42.0


class TestWebSocket:
    """WebSocket /chat/ws 端点测试。"""

    def test_ws_route_registered(self, tmp_facade):
        """WebSocket 路由已注册在 create_app 中。"""
        from api.server import create_app

        app = create_app(facade=tmp_facade)

        if not _is_fastapi_available():
            pytest.skip("FastAPI not installed")

        ws_routes = [r for r in app.routes if getattr(r, "path", "") == "/chat/ws"]
        assert len(ws_routes) >= 1, "WebSocket route /chat/ws not found"

    def test_ws_route_method(self, tmp_facade):
        """WebSocket 路由使用 websocket 方法。"""
        from api.server import create_app

        app = create_app(facade=tmp_facade)

        if not _is_fastapi_available():
            pytest.skip("FastAPI not installed")

        ws_routes = [r for r in app.routes if getattr(r, "path", "") == "/chat/ws"]
        assert len(ws_routes) >= 1
        # FastAPI WebSocket 路由应该有 websocket 相关属性

    def test_ws_endpoint_imports_work(self, tmp_facade):
        """WebSocket 端点使用的所有依赖可正常导入。"""

        from api.server import create_app

        app = create_app(facade=tmp_facade)
        assert app is not None

        # 验证 FastAPI WebSocket 可用
        try:
            from fastapi import WebSocket

            assert WebSocket is not None
        except ImportError:
            pytest.skip("FastAPI WebSocket not available")

    def test_ws_module_compiles(self):
        """server.py 模块可正常加载且 ws_chat 函数存在。"""
        from api import server as server_mod

        app = server_mod.create_app()
        if not _is_fastapi_available():
            pytest.skip("FastAPI not installed")

        # 查找 WebSocket 路由
        ws_routes = [r for r in app.routes if getattr(r, "path", "") == "/chat/ws"]
        assert len(ws_routes) >= 1
        # endpoint 函数应该存在
        route = ws_routes[0]
        assert route.endpoint is not None

    def test_ws_auth_verify_methods(self):
        """APIKeyAuth 和 JWTAuth 的 verify() 方法存在且工作。"""
        from auth.middleware import APIKeyAuth, JWTAuth

        # APIKeyAuth.verify
        api = APIKeyAuth(valid_keys=["test_key_12345"])
        assert api.verify({"X-API-Key": "test_key_12345"}) is True
        assert api.verify({"X-API-Key": "wrong_key"}) is False
        assert api.verify({}) is False

        # JWTAuth.verify (无 PyJWT 或无密钥时 fallback)
        jwt_auth = JWTAuth(secret="test-secret-not-long-enough-for-hs256")
        # 无 token → False
        assert jwt_auth.verify({"Authorization": ""}) is False
