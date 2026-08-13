"""熔断器实现

状态机：CLOSED（正常）→ OPEN（熔断）→ HALF_OPEN（试探）→ CLOSED（恢复）
每个 Tool 实例可绑定独立熔断器，失败次数达阈值后自动熔断。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from core.exceptions import CircuitBreakerOpen
from core.logger import logger


class CircuitBreaker:
    """线程安全的熔断器。"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = "CLOSED"  # CLOSED / OPEN / HALF_OPEN
        self._failures = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        return self._state

    # ---- 状态转换 ----

    def _trip(self) -> None:
        """触发熔断：CLOSED → OPEN。"""
        self._state = "OPEN"
        self._last_failure_time = time.time()
        logger.warning(
            f"Circuit breaker [{self.name}] TRIPPED → OPEN",
            extra={"failures": self._failures, "threshold": self.failure_threshold},
        )

    def _attempt_reset(self) -> None:
        """尝试恢复：OPEN → HALF_OPEN。"""
        if self._last_failure_time is None:
            return
        if time.time() - self._last_failure_time >= self.recovery_timeout:
            with self._lock:
                if self._state == "OPEN":
                    self._state = "HALF_OPEN"
                    self._half_open_calls = 0
                    logger.info(f"Circuit breaker [{self.name}] → HALF_OPEN")

    def _reset(self) -> None:
        """完全恢复：HALF_OPEN → CLOSED。"""
        with self._lock:
            self._state = "CLOSED"
            self._failures = 0
            self._half_open_calls = 0
            self._last_failure_time = None
            logger.info(f"Circuit breaker [{self.name}] → CLOSED (recovered)")

    # ---- 调用包装 ----

    def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """在熔断器保护下执行函数。"""

        # 1. 状态检查
        with self._lock:
            if self._state == "OPEN":
                self._attempt_reset()
                if self._state == "OPEN":
                    raise CircuitBreakerOpen(
                        f"熔断器 [{self.name}] 已打开（连续失败 {self._failures} 次），"
                        f"请等待 {self.recovery_timeout}s 后重试",
                        recoverable=True,
                    )

            if self._state == "HALF_OPEN" and self._half_open_calls >= self.half_open_max_calls:
                raise CircuitBreakerOpen(
                    f"熔断器 [{self.name}] 半开试探名额已满",
                    recoverable=True,
                )

            if self._state == "HALF_OPEN":
                self._half_open_calls += 1

        # 2. 执行函数
        try:
            result = func(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise

        # 3. 成功处理
        self._record_success()
        return result

    def _record_success(self) -> None:
        """记录成功。"""
        with self._lock:
            if self._state == "HALF_OPEN":
                self._reset()
            elif self._state == "CLOSED":
                self._failures = 0

    def _record_failure(self) -> None:
        """记录失败。"""
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.time()

            if self._state == "HALF_OPEN":
                # 半开状态再次失败，直接重新熔断
                self._trip()
            elif self._state == "CLOSED" and self._failures >= self.failure_threshold:
                self._trip()

    def __repr__(self) -> str:
        return f"<CircuitBreaker '{self.name}' state={self._state} failures={self._failures}>"


# ------------------------------------------------------------------------------
# 装饰器
# ------------------------------------------------------------------------------


def with_circuit_breaker(
    name: str | None = None,
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
) -> Callable:
    """装饰器：为函数绑定熔断器。

    用法:
        @with_circuit_breaker(name="web_search", failure_threshold=3)
        def search(query: str) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        cb_name = name or func.__name__
        cb = CircuitBreaker(
            name=cb_name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        )

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return cb.call(func, *args, **kwargs)

        # 把熔断器实例挂到 wrapper 上，方便外部观测
        wrapper._circuit_breaker = cb  # type: ignore[attr-defined]
        return wrapper

    return decorator
