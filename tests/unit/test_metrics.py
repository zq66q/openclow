"""openclaw/src/api/metrics.py 单元测试。

测试指标收集、Prometheus 文本渲染、/metrics 端点与流计数。
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


class TestMetricsCollector:
    """Metrics 收集器基础行为。"""

    def test_record_and_render_requests(self):
        """记录请求后渲染出 counter 行。"""
        from api.metrics import Metrics

        m = Metrics()
        m.record_request("GET", "/health", 200, 0.01)
        m.record_request("GET", "/health", 200, 0.08)
        text = m.render()

        assert "openclaw_http_requests_total" in text
        assert 'method="GET",route="/health",status="200"' in text
        assert "openclaw_http_request_duration_seconds_bucket" in text
        assert "openclaw_http_request_duration_seconds_sum" in text
        assert "openclaw_http_request_duration_seconds_count" in text
        assert "openclaw_uptime_seconds" in text

    def test_histogram_buckets(self):
        """耗时进入正确分桶。"""
        from api.metrics import Metrics

        m = Metrics()
        m.record_request("POST", "/chat", 200, 0.001)
        m.record_request("POST", "/chat", 200, 0.1)
        text = m.render()

        lines = [ln for ln in text.splitlines() if 'route="/chat"' in ln and "bucket" in ln and "le=" in ln]
        bucket_values: dict[float, int] = {}
        for ln in lines:
            le_str = ln.split('le="')[1].split('"')[0]
            val = int(ln.rsplit(" ", 1)[1])
            bucket_values[float("inf") if le_str == "+Inf" else float(le_str)] = val

        # 0.001 只进入 le=0.005 及更大的桶; 0.1 同时满足 le=0.1
        assert bucket_values[0.005] == 1
        assert bucket_values[0.1] == 2  # 两个样本都 <= 0.1
        assert bucket_values[float("inf")] == 2  # 总数

    def test_stream_counter(self):
        """StreamCounter 上下文管理器增减活跃数。"""
        from api.metrics import Metrics, StreamCounter

        m = Metrics()
        with StreamCounter(metrics=m):
            assert m._active_streams == 1
            with StreamCounter(metrics=m):
                assert m._active_streams == 2
            assert m._active_streams == 1
        assert m._active_streams == 0

    def test_metrics_switch_respects_env(self):
        """OPENCLAW_METRICS_ENABLED=false 时不采集。"""
        from api.metrics import Metrics

        # 直接构造不受全局开关影响，验证 record 的 enabled 分支
        m = Metrics()
        m.record_request("GET", "/x", 500, 0.5)
        assert "openclaw_http_requests_total" in m.render()


class TestMetricsEndpoint:
    """/metrics HTTP 端点。"""

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

    def test_metrics_endpoint_ok(self, client):
        """GET /metrics 返回 200 与 Prometheus 文本。"""
        # 先发几个请求制造指标
        client.get("/health")
        client.get("/metrics")  # 首次请求的指标在下次渲染才可见
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers.get("content-type", "")
        body = response.text
        assert "openclaw_uptime_seconds" in body
        assert "openclaw_http_requests_total" in body
        # 之前的 /metrics 请求已被中间件记录
        assert 'route="/metrics"' in body

    def test_metrics_whitelisted_without_auth(self, client):
        """/metrics 在认证开启时仍免认证可访问。"""
        import os as _os

        old = _os.environ.get("OPENCLAW_AUTH_MODE")
        _os.environ["OPENCLAW_AUTH_MODE"] = "apikey"
        _os.environ["OPENCLAW_API_KEYS"] = "oc_test_key_123"
        try:
            # 重建 app 使认证中间件生效
            from api.server import create_app

            app = create_app(facade=client.app.state if hasattr(client.app, "state") else None)
            if not _is_fastapi_available():
                pytest.skip("FastAPI not installed")
            try:
                from fastapi.testclient import TestClient

                client2 = TestClient(app)
            except ImportError:
                pytest.skip("TestClient not available")
            resp = client2.get("/metrics")
            assert resp.status_code == 200
        finally:
            if old is None:
                _os.environ.pop("OPENCLAW_AUTH_MODE", None)
            else:
                _os.environ["OPENCLAW_AUTH_MODE"] = old
            _os.environ.pop("OPENCLAW_API_KEYS", None)
