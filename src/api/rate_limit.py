"""API 限流中间件 — 基于内存固定窗口的速率限制。

用途:
    - 防止 API Key 暴力破解（未认证请求同样计数）
    - 防止刷接口耗尽 LLM 额度 / 拖垮后端

环境变量:
    OPENCLAW_RATE_LIMIT_ENABLED        — 是否启用（默认 true）
    OPENCLAW_RATE_LIMIT_PER_MINUTE     — 每分钟最大请求数（默认 120）
    OPENCLAW_RATE_LIMIT_WINDOW_SECONDS — 窗口长度秒数（默认 60）

限流维度:
    - 按客户端 IP + API Key（若携带 X-API-Key）组合计数
    - 未携带 Key 的请求按 IP 计数

注意:
    单进程内存实现。多 worker 部署时每个进程独立计数，
    实际上限会随 worker 数放大；如需精确限流建议引入 Redis。
"""

from __future__ import annotations

import hashlib
import os
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

try:
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
    from starlette.responses import JSONResponse

    _HAS_STARLETTE = True
except ImportError:
    _HAS_STARLETTE = False
    BaseHTTPMiddleware = object  # type: ignore[misc,assignment]
    JSONResponse = None  # type: ignore[misc,assignment]

# ── 常量 ──

_API_KEY_HEADER = "X-API-Key"

# 免限流路径（与认证白名单保持一致）
_WHITELIST_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


def _env_enabled() -> bool:
    """读取限流开关。"""
    return os.getenv("OPENCLAW_RATE_LIMIT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _env_per_minute() -> int:
    """读取每分钟最大请求数。"""
    try:
        return max(1, int(os.getenv("OPENCLAW_RATE_LIMIT_PER_MINUTE", "120")))
    except ValueError:
        return 120


def _env_window_seconds() -> int:
    """读取窗口长度。"""
    try:
        return max(1, int(os.getenv("OPENCLAW_RATE_LIMIT_WINDOW_SECONDS", "60")))
    except ValueError:
        return 60


# ── 固定窗口限流器 ──


class FixedWindowLimiter:
    """内存固定窗口限流器。

    每个 key 维护 (window_id, count)。当前窗口内计数达到上限即拒绝，
    进入下一窗口后自动重置。窗口由 ``int(now // window_seconds)`` 计算。

    Thread-safe 说明：GIL 保证单条 dict 读写原子，无需额外加锁；
    多 worker 进程间不共享（见模块 docstring）。
    """

    # 超过该条数后触发惰性清理，防止长期运行内存增长
    _MAX_ENTRIES = 100_000

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._counts: dict[str, tuple[int, int]] = {}

    @property
    def max_requests(self) -> int:
        return self._max

    @property
    def window_seconds(self) -> int:
        return self._window

    @property
    def _current_window_id(self) -> int:
        return int(time.time() // self._window)

    def check(self, key: str) -> bool:
        """检查并记录一次请求。返回 True 放行，False 超限。

        每次调用都会推进计数（无论是否超限都会记一次，
        避免超限后继续计数导致窗口内计数无限增长的问题——
        这里对超限请求也递增，使重试持续被拒直到窗口结束）。
        """
        if not key:
            key = "unknown"

        wid = self._current_window_id
        entry = self._counts.get(key)

        if entry is None or entry[0] != wid:
            # 新窗口：重置计数
            self._maybe_cleanup(wid)
            self._counts[key] = (wid, 1)
            return True

        _w, count = entry
        if count >= self._max:
            return False
        self._counts[key] = (wid, count + 1)
        return True

    def retry_after(self) -> int:
        """距离当前窗口结束的秒数（用于 Retry-After 响应头）。"""
        wid = self._current_window_id
        window_end = (wid + 1) * self._window
        return max(1, int(window_end - time.time()))

    def _maybe_cleanup(self, current_wid: int) -> None:
        """条目过多时清理非当前窗口的记录。"""
        if len(self._counts) < self._MAX_ENTRIES:
            return
        stale = [k for k, (w, _c) in self._counts.items() if w != current_wid]
        for k in stale:
            self._counts.pop(k, None)

    def clear(self) -> None:
        """清空所有计数（测试与运维用）。"""
        self._counts.clear()


def build_limiter() -> FixedWindowLimiter:
    """根据环境变量构建限流器。"""
    return FixedWindowLimiter(
        max_requests=_env_per_minute(),
        window_seconds=_env_window_seconds(),
    )


# ── Starlette 中间件 ──


class RateLimitMiddleware(BaseHTTPMiddleware):
    """基于 FixedWindowLimiter 的 ASGI 中间件。

    放在认证中间件外层：未认证请求同样限流（防暴力破解），
    但白名单路径（/health 等）直接放行。
    """

    def __init__(self, app: Any, limiter: FixedWindowLimiter | None = None, enabled: bool | None = None) -> None:
        super().__init__(app)
        self._limiter = limiter or build_limiter()
        self._enabled = _env_enabled() if enabled is None else enabled

    def _build_key(self, request: Any) -> str:
        """组合 IP + API Key hash 作为限流维度。"""
        ip = request.client.host if request.client is not None else "unknown"
        key = request.headers.get(_API_KEY_HEADER, "") or request.headers.get("x-api-key", "")
        if key:
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            return f"{ip}:{digest}"
        return ip

    async def dispatch(self, request: Any, call_next: RequestResponseEndpoint) -> Any:
        if not self._enabled:
            return await call_next(request)

        if request.url.path in _WHITELIST_PATHS:
            return await call_next(request)

        client_key = self._build_key(request)
        if not self._limiter.check(client_key):
            retry = self._limiter.retry_after()
            logger.warning(
                "Rate limit exceeded",
                extra={"client_key": client_key, "path": request.url.path, "retry_after": retry},
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests", "retry_after": retry},
                headers={"Retry-After": str(retry)},
            )

        return await call_next(request)
