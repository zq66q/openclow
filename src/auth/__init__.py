"""认证模块 — API Key + JWT 双轨认证中间件。

设计:
    - 无外部依赖时仅提供 API Key 验证（标准库）
    - 有 PyJWT 时增加 JWT Token 签发与验证
    - FastAPI 依赖函数可插拔，未安装时自动降级为 no-op

用法:
    from auth.middleware import api_key_guard
    from fastapi import Depends

    @router.get("/admin", dependencies=[Depends(api_key_guard)])
    async def admin():
        ...
"""

from __future__ import annotations

from auth.middleware import (
    APIKeyAuth,
    JWTAuth,
    api_key_guard,
    generate_api_key,
    jwt_guard,
)

__all__ = [
    "APIKeyAuth",
    "JWTAuth",
    "api_key_guard",
    "jwt_guard",
    "generate_api_key",
]
