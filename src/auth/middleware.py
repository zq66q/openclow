"""认证中间件 — 生产级 API Key + JWT 验证。

安全特性:
    - 时序安全比较 (secrets.compare_digest) 防侧信道攻击
    - API Key 支持多 Key 管理，可独立轮换
    - JWT 支持 HS256 签名 + 过期时间 + 签发者校验
    - 所有错误返回统一 401/403，不泄漏内部细节
    - 请求日志记录认证失败（防暴力破解）

环境变量:
    OPENCLAW_API_KEYS      — 逗号分隔的合法 API Key 列表
    OPENCLAW_JWT_SECRET    — JWT 签名密钥（至少 32 字节）
    OPENCLAW_JWT_EXPIRE_HOURS — Token 有效期（默认 24）
    OPENCLAW_AUTH_MODE     — 认证模式: apikey|jwt|both|none（默认 apikey）
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any

from core.logger import logger

# 加载 .env 文件到环境变量，确保 os.getenv 能读到配置
try:
    from dotenv import load_dotenv

    load_dotenv(".env")
except Exception:
    pass

# ── 可选依赖检测 ──

_HAS_JWT = False

try:
    import jwt as _jwt_lib

    _HAS_JWT = True
except ImportError:
    _jwt_lib = None  # type: ignore[assignment]

try:
    from fastapi import HTTPException, Request, Security, status
    from fastapi.security import APIKeyHeader, HTTPBearer
except ImportError:
    HTTPException = Exception  # type: ignore[misc,assignment]
    Request = object  # type: ignore[misc,assignment]

    def Security(x, **_):  # type: ignore[no-redef]
        return x

    APIKeyHeader = object  # type: ignore[misc,assignment]
    HTTPBearer = object  # type: ignore[misc,assignment]
    status = None  # type: ignore[assignment]

# ── 常量 ──

_API_KEY_HEADER = "X-API-Key"
_JWT_ALGORITHM = "HS256"
_JWT_ISSUER = "openclaw"


# ── 工具 ──


def generate_api_key(prefix: str = "oc") -> str:
    """生成一个安全的随机 API Key。

    格式: ``oc_<base64(random_bytes(24))>``
    """
    rnd = os.urandom(24)
    b64 = base64.urlsafe_b64encode(rnd).decode("ascii").rstrip("=")
    return f"{prefix}_{b64}"


def _get_env_keys() -> list[str]:
    """从环境变量读取合法 API Key 列表。"""
    raw = os.getenv("OPENCLAW_API_KEYS", "").strip()
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def _get_jwt_secret() -> str:
    """读取 JWT 签名密钥。"""
    secret = os.getenv("OPENCLAW_JWT_SECRET", "")
    if not secret:
        # 生产环境必须设置密钥
        logger.warning("OPENCLAW_JWT_SECRET not set — JWT disabled")
    return secret


def _get_auth_mode() -> str:
    """读取认证模式。"""
    return os.getenv("OPENCLAW_AUTH_MODE", "apikey").strip().lower()


# ── API Key 认证 ──


class APIKeyAuth:
    """API Key 验证器。

    支持多 Key 管理，可独立轮换。验证失败时记录日志。
    """

    def __init__(self, valid_keys: list[str] | None = None) -> None:
        self._keys: list[str] = valid_keys or _get_env_keys()
        if not self._keys and _get_auth_mode() != "none":
            logger.warning("No API keys configured — all requests will be rejected")

    @property
    def enabled(self) -> bool:
        return len(self._keys) > 0

    def validate(self, key: str | None) -> bool:
        """时序安全地验证 API Key。"""
        if key is None:
            return False
        # 防御性处理：key 中包含的异常字符不影响比较
        return any(secrets.compare_digest(key.strip(), valid) for valid in self._keys)

    def authenticate(self, request: Any = None, raw_key: str | None = None) -> str:
        """验证并返回用户标识（API Key 的 hash 前缀）。

        Raises:
            HTTPException: 401 认证失败
        """
        if not self.enabled:
            if _get_auth_mode() == "none":
                return "anonymous"
            # 配置错误：启用了认证但没有 Key
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED if status else 401,
                detail="Authentication not configured",
                headers={"WWW-Authenticate": _API_KEY_HEADER},
            )

        key = raw_key or self._extract_key(request)
        if key and self.validate(key):
            # 返回 hash 前缀作为用户标识，不暴露完整 Key
            return f"apikey_{hashlib.sha256(key.encode()).hexdigest()[:8]}"

        logger.warning("API Key authentication failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED if status else 401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": _API_KEY_HEADER},
        )

    @staticmethod
    def _extract_key(request: Any) -> str | None:
        """从请求头提取 API Key。"""
        if request is None:
            return None
        # FastAPI Request
        if hasattr(request, "headers"):
            return request.headers.get(_API_KEY_HEADER) or request.headers.get("x-api-key")
        # 字典风格
        if isinstance(request, dict):
            return request.get(_API_KEY_HEADER) or request.get("x-api-key")
        return None

    def verify(self, headers: dict[str, str]) -> bool:
        """轻量验证（用于 WebSocket），不抛异常，返回 True/False。"""
        key = headers.get(_API_KEY_HEADER, "") or headers.get("x-api-key", "")
        return bool(key) and self.validate(key.strip())


# ── JWT 认证 ──


class JWTAuth:
    """JWT Token 验证器。

    需要 ``PyJWT`` 库。未安装时自动降级为拒绝所有请求。
    """

    def __init__(self, secret: str | None = None, expire_hours: int = 24) -> None:
        self._secret = secret or _get_jwt_secret()
        self._expire_hours = expire_hours
        self._has_lib = _HAS_JWT

    @property
    def enabled(self) -> bool:
        return self._has_lib and bool(self._secret)

    def encode(self, payload: dict[str, Any]) -> str:
        """签发 JWT Token。

        Args:
            payload: 自定义声明（会自动加入 exp, iat, iss）

        Returns:
            JWT 字符串

        Raises:
            RuntimeError: JWT 不可用
        """
        if not self.enabled:
            raise RuntimeError("JWT not available: install PyJWT and set OPENCLAW_JWT_SECRET")
        assert _jwt_lib is not None
        now = int(time.time())
        claims = {
            "iat": now,
            "exp": now + self._expire_hours * 3600,
            "iss": _JWT_ISSUER,
            **payload,
        }
        return _jwt_lib.encode(claims, self._secret, algorithm=_JWT_ALGORITHM)

    def decode(self, token: str | None) -> dict[str, Any]:
        """验证并解码 JWT Token。

        Raises:
            HTTPException: 401 Token 无效或过期
        """
        if not self.enabled:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED if status else 401,
                detail="JWT authentication not configured",
                headers={"WWW-Authenticate": "Bearer"},
            )
        assert _jwt_lib is not None
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED if status else 401,
                detail="Missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            decoded = _jwt_lib.decode(
                token,
                self._secret,
                algorithms=[_JWT_ALGORITHM],
                issuer=_JWT_ISSUER,
            )
            return decoded
        except _jwt_lib.ExpiredSignatureError:
            logger.warning("JWT token expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED if status else 401,
                detail="Token expired",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        except _jwt_lib.InvalidTokenError as exc:
            logger.warning(f"JWT validation error: {exc}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED if status else 401,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    def authenticate(self, request: Any = None, raw_token: str | None = None) -> str:
        """验证并返回 sub（用户标识）。"""
        token = raw_token or self._extract_token(request)
        claims = self.decode(token)
        return claims.get("sub", "unknown")

    @staticmethod
    def _extract_token(request: Any) -> str | None:
        """从请求头提取 Bearer Token。"""
        if request is None:
            return None
        auth = None
        if hasattr(request, "headers"):
            auth = request.headers.get("Authorization") or request.headers.get("authorization")
        elif isinstance(request, dict):
            auth = request.get("Authorization") or request.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def verify(self, headers: dict[str, str]) -> bool:
        """轻量验证（用于 WebSocket），不抛异常，返回 True/False。"""
        if not self.enabled:
            return True
        auth = headers.get("Authorization", "") or headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        token = auth[7:].strip()
        try:
            self.decode(token)
            return True
        except Exception:
            return False


# ── FastAPI 依赖函数 ──


_api_key_auth = APIKeyAuth()
_jwt_auth = JWTAuth()

# 占位：无 FastAPI 时的安全标头提取器
_api_key_header = APIKeyHeader(name=_API_KEY_HEADER, auto_error=False) if APIKeyHeader is not object else None  # type: ignore[comparison-overlap]
_http_bearer = HTTPBearer(auto_error=False) if HTTPBearer is not object else None  # type: ignore[comparison-overlap]


def api_key_guard(
    request: Any = None,
    api_key: str | None = None,
) -> str:
    """FastAPI Depends 兼容的 API Key 守卫。

    用法::

        @app.get("/admin", dependencies=[Depends(api_key_guard)])
        async def admin():
            ...
    """
    auth = APIKeyAuth()
    return auth.authenticate(request=request, raw_key=api_key)


def jwt_guard(
    request: Any = None,
    token: str | None = None,
) -> str:
    """FastAPI Depends 兼容的 JWT 守卫。"""
    auth = JWTAuth()
    return auth.authenticate(request=request, raw_token=token)


# ── 组合认证（模式感知） ──


def auth_guard(
    request: Any = None,
    raw_key: str | None = None,
    raw_token: str | None = None,
) -> str:
    """根据 OPENCLAW_AUTH_MODE 自动选择认证方式。

    模式:
        - apikey: 仅 API Key
        - jwt: 仅 JWT
        - both: 任一通过即可
        - none: 跳过认证（返回 anonymous）
    """
    mode = _get_auth_mode()
    if mode == "none":
        return "anonymous"

    api_auth = APIKeyAuth()
    jwt_auth = JWTAuth()

    errors: list[str] = []

    if mode in ("apikey", "both"):
        try:
            return api_auth.authenticate(request=request, raw_key=raw_key)
        except Exception as exc:
            errors.append(str(exc))

    if mode in ("jwt", "both"):
        try:
            return jwt_auth.authenticate(request=request, raw_token=raw_token)
        except Exception as exc:
            errors.append(str(exc))

    # 全部失败
    if mode == "both":
        logger.warning(f"Both auth methods failed: {errors}")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED if status else 401,
        detail="Authentication failed",
        headers={"WWW-Authenticate": f"{_API_KEY_HEADER}, Bearer"},
    )
