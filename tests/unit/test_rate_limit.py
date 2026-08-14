"""openclaw/src/api/rate_limit.py 单元测试。

覆盖 FixedWindowLimiter（固定窗口计数逻辑）与 RateLimitMiddleware（ASGI 中间件）。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest

from api.rate_limit import (
    FixedWindowLimiter,
    RateLimitMiddleware,
    _env_enabled,
    _env_per_minute,
    _env_window_seconds,
)


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


# ── FixedWindowLimiter ──


class TestFixedWindowLimiter:
    def test_allow_until_limit(self):
        """达到上限前放行，超出后拒绝。"""
        limiter = FixedWindowLimiter(max_requests=3, window_seconds=60)
        assert limiter.check("k1") is True
        assert limiter.check("k1") is True
        assert limiter.check("k1") is True
        assert limiter.check("k1") is False

    def test_max_requests_one(self):
        limiter = FixedWindowLimiter(max_requests=1, window_seconds=60)
        assert limiter.check("k") is True
        assert limiter.check("k") is False

    def test_independent_keys(self):
        """不同 key 各自独立计数。"""
        limiter = FixedWindowLimiter(max_requests=2, window_seconds=60)
        assert limiter.check("a") is True
        assert limiter.check("a") is True
        assert limiter.check("b") is True  # b 独立
        assert limiter.check("a") is False
        assert limiter.check("b") is True

    def test_window_reset(self, monkeypatch):
        """进入下一窗口后计数重置。"""
        limiter = FixedWindowLimiter(max_requests=2, window_seconds=60)
        fake_now = {"t": 100.0}
        monkeypatch.setattr(time, "time", lambda: fake_now["t"])

        assert limiter.check("k") is True
        assert limiter.check("k") is True
        assert limiter.check("k") is False

        fake_now["t"] = 160.0  # 进入下一窗口
        assert limiter.check("k") is True
        assert limiter.check("k") is True
        assert limiter.check("k") is False

    def test_retry_after_positive(self, monkeypatch):
        """Retry-After 始终为正数。"""
        limiter = FixedWindowLimiter(max_requests=1, window_seconds=60)
        monkeypatch.setattr(time, "time", lambda: 30.0)
        assert limiter.retry_after() > 0

    def test_empty_key_isolated(self):
        """空 key 归入 unknown，不与正常 key 冲突。"""
        limiter = FixedWindowLimiter(max_requests=1, window_seconds=60)
        assert limiter.check("") is True
        assert limiter.check("") is False
        assert limiter.check("other") is True  # 独立

    def test_clear(self):
        limiter = FixedWindowLimiter(max_requests=1, window_seconds=60)
        assert limiter.check("k") is True
        limiter.clear()
        assert limiter.check("k") is True


# ── 环境变量读取 ──


class TestEnvHelpers:
    def test_env_per_minute_default(self, monkeypatch):
        monkeypatch.delenv("OPENCLAW_RATE_LIMIT_PER_MINUTE", raising=False)
        assert _env_per_minute() == 120

    def test_env_per_minute_custom(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_RATE_LIMIT_PER_MINUTE", "500")
        assert _env_per_minute() == 500

    def test_env_per_minute_invalid(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_RATE_LIMIT_PER_MINUTE", "abc")
        assert _env_per_minute() == 120

    def test_env_window_seconds_default(self, monkeypatch):
        monkeypatch.delenv("OPENCLAW_RATE_LIMIT_WINDOW_SECONDS", raising=False)
        assert _env_window_seconds() == 60

    def test_env_enabled_default(self, monkeypatch):
        monkeypatch.delenv("OPENCLAW_RATE_LIMIT_ENABLED", raising=False)
        assert _env_enabled() is True

    def test_env_enabled_disabled(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_RATE_LIMIT_ENABLED", "false")
        assert _env_enabled() is False


# ── RateLimitMiddleware ──


class TestRateLimitMiddleware:
    def _make_client(self, limiter: FixedWindowLimiter | None = None, enabled: bool = True):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/hello")
        async def hello():
            return {"msg": "ok"}

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        app.add_middleware(RateLimitMiddleware, limiter=limiter, enabled=enabled)
        return TestClient(app)

    def test_normal_request(self):
        if not (_is_fastapi_available() and _is_httpx_available()):
            pytest.skip("FastAPI/httpx not installed")
        client = self._make_client(limiter=FixedWindowLimiter(3, 60), enabled=True)
        for _ in range(3):
            resp = client.get("/hello")
            assert resp.status_code == 200

    def test_limit_exceeded_returns_429(self):
        if not (_is_fastapi_available() and _is_httpx_available()):
            pytest.skip("FastAPI/httpx not installed")
        client = self._make_client(limiter=FixedWindowLimiter(1, 60), enabled=True)
        assert client.get("/hello").status_code == 200

        resp = client.get("/hello")
        assert resp.status_code == 429
        assert resp.json()["detail"] == "Too many requests"
        assert "Retry-After" in resp.headers
        assert int(resp.headers["Retry-After"]) > 0

    def test_whitelist_bypass(self):
        """白名单路径（/health）不受限。"""
        if not (_is_fastapi_available() and _is_httpx_available()):
            pytest.skip("FastAPI/httpx not installed")
        client = self._make_client(limiter=FixedWindowLimiter(1, 60), enabled=True)
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200  # 第 2 次仍放行

    def test_disabled(self):
        """关闭限流后不计数。"""
        if not (_is_fastapi_available() and _is_httpx_available()):
            pytest.skip("FastAPI/httpx not installed")
        client = self._make_client(limiter=FixedWindowLimiter(1, 60), enabled=False)
        assert client.get("/hello").status_code == 200
        assert client.get("/hello").status_code == 200  # 第 2 次也放行

    def test_build_key_differs_by_api_key(self):
        """不同 API Key 各自独立计数，同一 Key 超限。"""
        if not (_is_fastapi_available() and _is_httpx_available()):
            pytest.skip("FastAPI/httpx not installed")
        client = self._make_client(limiter=FixedWindowLimiter(1, 60), enabled=True)

        assert client.get("/hello", headers={"X-API-Key": "key-aaa"}).status_code == 200
        assert client.get("/hello", headers={"X-API-Key": "key-bbb"}).status_code == 200  # 独立
        assert client.get("/hello", headers={"X-API-Key": "key-aaa"}).status_code == 429  # 同 key 超限
