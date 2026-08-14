"""轻量可观测性指标 — Prometheus 文本格式（零第三方依赖）。

提供:
- HTTP 请求计数 / 耗时直方图（MetricsMiddleware 自动采集）
- 活跃流式连接数（SSE / WebSocket，通过 active_stream 上下文管理器）
- 服务运行时长

不依赖 prometheus_client，输出标准 Prometheus 文本协议，可直接被
Prometheus / Grafana / VictoriaMetrics 抓取。
"""

from __future__ import annotations

import threading
import time
from typing import Any

# 耗时直方图分桶（秒），与 Prometheus 默认值对齐
_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_METRICS_ENABLED = True


class Metrics:
    """进程内指标收集器，线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.time()
        # (method, route, status) -> count
        self._request_counts: dict[tuple[str, str, int], int] = {}
        # (method, route, le) -> count
        self._durations: dict[tuple[str, str, float], int] = {}
        # 直方图样本总数 (method, route)
        self._duration_total: dict[tuple[str, str], int] = {}
        self._duration_sum: dict[tuple[str, str], float] = {}
        self._active_streams = 0
        self._stream_total = 0

    def record_request(self, method: str, route: str, status: int, elapsed: float) -> None:
        """记录一次 HTTP 请求的结果与耗时。"""
        if not _METRICS_ENABLED:
            return
        key = (method, route, status)
        with self._lock:
            self._request_counts[key] = self._request_counts.get(key, 0) + 1
            rk = (method, route)
            self._duration_sum[rk] = self._duration_sum.get(rk, 0.0) + elapsed
            self._duration_total[rk] = self._duration_total.get(rk, 0) + 1
            for le in _BUCKETS:
                if elapsed <= le:
                    self._durations[(method, route, le)] = self._durations.get((method, route, le), 0) + 1

    def enter_stream(self) -> None:
        """流式连接建立时调用。"""
        with self._lock:
            self._active_streams += 1
            self._stream_total += 1

    def exit_stream(self) -> None:
        """流式连接结束时调用。"""
        with self._lock:
            if self._active_streams > 0:
                self._active_streams -= 1

    def render(self) -> str:
        """渲染为 Prometheus 文本格式。"""
        now = time.time()
        lines: list[str] = [
            "# HELP openclaw_uptime_seconds Seconds since service start.",
            "# TYPE openclaw_uptime_seconds gauge",
            f"openclaw_uptime_seconds {now - self._started:.3f}",
            "",
            "# HELP openclaw_http_requests_total Total HTTP requests by method, route and status.",
            "# TYPE openclaw_http_requests_total counter",
        ]
        with self._lock:
            for (method, route, status), count in sorted(self._request_counts.items()):
                lines.append(
                    f'openclaw_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
                )

            lines += [
                "",
                "# HELP openclaw_http_request_duration_seconds HTTP request latency.",
                "# TYPE openclaw_http_request_duration_seconds histogram",
            ]
            for method, route in sorted(self._duration_total):
                lines.append(
                    f'openclaw_http_request_duration_seconds_bucket{{method="{method}",route="{route}",le="+Inf"}} {self._duration_total[(method, route)]}'
                )
                for le in _BUCKETS:
                    cnt = self._durations.get((method, route, le), 0)
                    le_s = f"{le:g}"
                    lines.append(
                        f'openclaw_http_request_duration_seconds_bucket{{method="{method}",route="{route}",le="{le_s}"}} {cnt}'
                    )
                lines.append(
                    f'openclaw_http_request_duration_seconds_sum{{method="{method}",route="{route}"}} {self._duration_sum[(method, route)]:.6f}'
                )
                lines.append(
                    f'openclaw_http_request_duration_seconds_count{{method="{method}",route="{route}"}} {self._duration_total[(method, route)]}'
                )

            lines += [
                "",
                "# HELP openclaw_active_streams Currently active SSE/WebSocket streams.",
                "# TYPE openclaw_active_streams gauge",
                f"openclaw_active_streams {self._active_streams}",
                "# HELP openclaw_streams_total Total streams ever established.",
                "# TYPE openclaw_streams_total counter",
                f"openclaw_streams_total {self._stream_total}",
            ]
        return "\n".join(lines) + "\n"


# 进程内单例
_metrics = Metrics()


def get_metrics() -> Metrics:
    """获取全局指标实例。"""
    return _metrics


def metrics_endpoint() -> Any:
    """返回 /metrics 路由的响应函数（延迟导入，避免无 FastAPI 时报错）。"""
    from fastapi.responses import PlainTextResponse

    async def handler() -> PlainTextResponse:
        return PlainTextResponse(_metrics.render(), media_type="text/plain; version=0.0.4")

    return handler


class MetricsMiddleware:
    """Starlette 中间件：统计每个 HTTP 请求的方法/路由/状态码/耗时。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request

        request = Request(scope)
        start = time.monotonic()
        status_holder: dict[str, int] = {"status": 0}

        async def _send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send_wrapper)
        finally:
            route = getattr(request.scope.get("route", None), "path", None) or request.url.path
            elapsed = time.monotonic() - start
            _metrics.record_request(request.method, route, status_holder["status"], elapsed)


class StreamCounter:
    """上下文管理器：统计 SSE / WebSocket 活跃连接数。"""

    def __init__(self, metrics: Metrics | None = None) -> None:
        self._metrics = metrics or _metrics

    def __enter__(self) -> StreamCounter:
        self._metrics.enter_stream()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._metrics.exit_stream()
