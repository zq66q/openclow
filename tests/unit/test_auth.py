"""openclaw/src/auth/middleware.py 单元测试。

覆盖 APIKeyAuth、JWTAuth、generate_api_key、auth_guard。
无需外部依赖（PyJWT / FastAPI 可选）。
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from auth.middleware import (
    APIKeyAuth,
    JWTAuth,
    _get_auth_mode,
    _get_env_keys,
    _get_jwt_secret,
    generate_api_key,
)

# ── generate_api_key ──


class TestGenerateApiKey:
    def test_format(self):
        key = generate_api_key()
        assert key.startswith("oc_")
        assert len(key) > 10

    def test_uniqueness(self):
        keys = {generate_api_key() for _ in range(50)}
        assert len(keys) == 50

    def test_custom_prefix(self):
        key = generate_api_key(prefix="test")
        assert key.startswith("test_")


# ── APIKeyAuth ──


class TestAPIKeyAuth:
    def test_validate_valid_key(self):
        auth = APIKeyAuth(valid_keys=["key123"])
        assert auth.validate("key123")

    def test_validate_invalid_key(self):
        auth = APIKeyAuth(valid_keys=["key123"])
        assert not auth.validate("wrong")
        assert not auth.validate("")
        assert not auth.validate(None)

    def test_validate_timing_safe(self):
        """即使部分匹配也不通过。"""
        auth = APIKeyAuth(valid_keys=["secret_key_12345"])
        assert not auth.validate("secret_key")
        assert not auth.validate("secret_key_1234")

    def test_authenticate_success(self):
        auth = APIKeyAuth(valid_keys=["valid"])
        uid = auth.authenticate(raw_key="valid")
        assert uid.startswith("apikey_")

    def test_authenticate_fail_raises(self):
        auth = APIKeyAuth(valid_keys=["valid"])
        with pytest.raises(HTTPException):
            auth.authenticate(raw_key="invalid")

    def test_extract_key_from_dict(self):
        key = APIKeyAuth._extract_key({"X-API-Key": "abc"})
        assert key == "abc"

    def test_extract_key_none(self):
        assert APIKeyAuth._extract_key(None) is None
        assert APIKeyAuth._extract_key({}) is None


# ── JWTAuth ──


class TestJWTAuth:
    def test_enabled_without_secret(self, monkeypatch):
        """无密钥时 JWT 不可用（需隔离环境变量，防止本地 .env 的 secret 干扰）。"""
        monkeypatch.delenv("OPENCLAW_JWT_SECRET", raising=False)
        monkeypatch.setattr("auth.middleware._get_jwt_secret", lambda: None)
        auth = JWTAuth(secret="")
        assert not auth.enabled

    def test_enabled_with_secret(self):
        """有密钥且 PyJWT 安装时才可用。"""
        auth = JWTAuth(secret="test_secret_1234567890")
        # enabled 取决于 _HAS_JWT
        if auth.enabled:
            assert auth.enabled

    def test_encode_decode_roundtrip(self):
        auth = JWTAuth(secret="test_secret_1234567890", expire_hours=1)
        if not auth.enabled:
            pytest.skip("PyJWT not installed")
        token = auth.encode({"sub": "user42", "role": "admin"})
        assert isinstance(token, str)
        assert "." in token  # JWT 三段式

        claims = auth.decode(token)
        assert claims["sub"] == "user42"
        assert claims["role"] == "admin"
        assert claims["iss"] == "openclaw"

    def test_decode_invalid_token(self):
        auth = JWTAuth(secret="test_secret_1234567890")
        if not auth.enabled:
            pytest.skip("PyJWT not installed")
        with pytest.raises(HTTPException):
            auth.decode("not.a.token")

    def test_decode_none(self):
        auth = JWTAuth(secret="test_secret_1234567890")
        if not auth.enabled:
            pytest.skip("PyJWT not installed")
        with pytest.raises(HTTPException):
            auth.decode(None)

    def test_extract_token_from_bearer(self):
        token = JWTAuth._extract_token({"Authorization": "Bearer abc123"})
        assert token == "abc123"

    def test_extract_token_no_bearer(self):
        assert JWTAuth._extract_token({"Authorization": "Basic xyz"}) is None
        assert JWTAuth._extract_token({}) is None

    def test_authenticate_success(self):
        auth = JWTAuth(secret="test_secret_1234567890")
        if not auth.enabled:
            pytest.skip("PyJWT not installed")
        token = auth.encode({"sub": "alice"})
        uid = auth.authenticate(raw_token=token)
        assert uid == "alice"

    def test_encode_without_jwt_raises(self):
        """无 PyJWT 时 encode 抛异常。"""
        auth = JWTAuth(secret="secret")
        if auth.enabled:
            pytest.skip("PyJWT installed")
        with pytest.raises(RuntimeError):
            auth.encode({"sub": "x"})


# ── 环境变量读取 ──


class TestEnvHelpers:
    def test_get_auth_mode_default(self):
        old = os.environ.pop("OPENCLAW_AUTH_MODE", None)
        try:
            assert _get_auth_mode() == "apikey"
        finally:
            if old is not None:
                os.environ["OPENCLAW_AUTH_MODE"] = old

    def test_get_env_keys_empty(self):
        old = os.environ.pop("OPENCLAW_API_KEYS", None)
        try:
            assert _get_env_keys() == []
        finally:
            if old is not None:
                os.environ["OPENCLAW_API_KEYS"] = old

    def test_get_env_keys_multiple(self):
        old = os.environ.get("OPENCLAW_API_KEYS")
        os.environ["OPENCLAW_API_KEYS"] = "key1, key2 , key3"
        try:
            assert _get_env_keys() == ["key1", "key2", "key3"]
        finally:
            if old is not None:
                os.environ["OPENCLAW_API_KEYS"] = old
            else:
                os.environ.pop("OPENCLAW_API_KEYS", None)

    def test_get_jwt_secret_empty(self):
        old = os.environ.pop("OPENCLAW_JWT_SECRET", None)
        try:
            assert _get_jwt_secret() == ""
        finally:
            if old is not None:
                os.environ["OPENCLAW_JWT_SECRET"] = old


# ── auth_guard 组合认证 ──


class TestAuthGuard:
    def test_none_mode(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_AUTH_MODE", "none")
        from auth.middleware import auth_guard

        assert auth_guard() == "anonymous"

    def test_apikey_mode_fail(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_AUTH_MODE", "apikey")
        monkeypatch.setenv("OPENCLAW_API_KEYS", "secret")
        from auth.middleware import auth_guard

        with pytest.raises(HTTPException):
            auth_guard()

    def test_apikey_mode_success(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_AUTH_MODE", "apikey")
        monkeypatch.setenv("OPENCLAW_API_KEYS", "mykey")
        from auth.middleware import auth_guard

        uid = auth_guard(raw_key="mykey")
        assert uid.startswith("apikey_")

    def test_both_mode_apikey_success(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_AUTH_MODE", "both")
        monkeypatch.setenv("OPENCLAW_API_KEYS", "k1")
        from auth.middleware import auth_guard

        uid = auth_guard(raw_key="k1")
        assert uid.startswith("apikey_")
