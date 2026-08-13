"""全局中间件链

洋葱模型：请求 → [参数校验] → [权限预检] → [审计开始] → [trace_id 注入]
          → Agent 执行 → [审计结束] → [响应脱敏] → response

每个中间件可以:
    - pass:   继续下一个中间件
    - reject: 终止链，抛出异常
    - modify: 修改 request / response 后继续
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from core.exceptions import ToolParamError
from core.logger import logger, set_trace_id

# ------------------------------------------------------------------------------
# 中间件抽象基类
# ------------------------------------------------------------------------------


class Middleware:
    """单个中间件。子类必须实现 process()。"""

    name: str = ""

    def process(self, request: dict[str, Any]) -> dict[str, Any]:
        """处理请求。返回修改后的 request（或原样返回）。

        如需拒绝请求，直接抛出异常。
        """
        raise NotImplementedError("Middleware subclass must implement process()")

    def on_complete(
        self, request: dict[str, Any], response: dict[str, Any] | None, error: Exception | None = None
    ) -> None:
        """请求完成后的回调（无论成功/失败都会调用）。子类可覆盖。"""
        pass


# ------------------------------------------------------------------------------
# 中间件链
# ------------------------------------------------------------------------------


class MiddlewareChain:
    """责任链模式：按顺序执行一组中间件。"""

    def __init__(self) -> None:
        self._middlewares: list[Middleware] = []

    def add(self, mw: Middleware) -> MiddlewareChain:
        """链式添加中间件。"""
        self._middlewares.append(mw)
        return self

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        """顺序执行所有中间件，返回最终 request。"""
        for mw in self._middlewares:
            logger.debug(f"Middleware [{mw.name}] processing")
            request = mw.process(request)
        return request

    def run_with_cleanup(
        self,
        request: dict[str, Any],
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """完整生命周期：中间件 → handler → 中间件 cleanup。"""
        start_time = time.time()
        response: dict[str, Any] | None = None
        error: Exception | None = None

        try:
            request = self.run(request)
            response = handler(request)
        except Exception as exc:
            error = exc
            raise
        finally:
            elapsed = time.time() - start_time
            for mw in self._middlewares:
                try:
                    mw.on_complete(request, response, error)
                except Exception:
                    logger.exception(f"Middleware [{mw.name}] cleanup failed")
            if response is not None:
                response["_meta"] = response.get("_meta", {})
                response["_meta"]["elapsed_ms"] = round(elapsed * 1000, 2)

        return response  # type: ignore[return-value]


# ------------------------------------------------------------------------------
# 内置中间件实现
# ------------------------------------------------------------------------------


class TraceIdInjector(Middleware):
    """为每个请求生成/注入 trace_id，贯穿整条调用链。"""

    name = "trace_id"

    def process(self, request: dict[str, Any]) -> dict[str, Any]:
        # 如果请求头里已有 trace_id（如前端传入），复用之；否则生成新的
        tid = request.get("trace_id")
        if not tid:
            tid = str(uuid.uuid4())[:16]
        set_trace_id(tid)
        request["trace_id"] = tid
        logger.info("Request start", extra={"trace_id": tid, "user_id": request.get("user_id")})
        return request

    def on_complete(
        self, request: dict[str, Any], response: dict[str, Any] | None, error: Exception | None = None
    ) -> None:
        tid = request.get("trace_id", "unknown")
        status = "error" if error else "success"
        logger.info(f"Request {status}", extra={"trace_id": tid})


class ParamValidator(Middleware):
    """请求参数基础校验（非空、类型、长度）。"""

    name = "param_validator"

    def process(self, request: dict[str, Any]) -> dict[str, Any]:
        # 校验必传字段
        if "messages" in request and not request["messages"]:
            raise ToolParamError("messages 不能为空列表")

        user_id = request.get("user_id")
        if user_id is not None and (not isinstance(user_id, str) or len(user_id) > 64):
            raise ToolParamError("user_id 必须是长度不超过 64 的字符串")

        # 限流参数范围
        temperature = request.get("temperature")
        if temperature is not None and not (0.0 <= float(temperature) <= 2.0):
            raise ToolParamError("temperature 必须在 0.0 ~ 2.0 之间")

        return request


class PermissionCheck(Middleware):
    """简易 RBAC 权限预检。"""

    name = "permission"

    # 危险操作清单 → 需要额外权限
    DANGEROUS_INTENTS = {"delete", "batch_update", "system_exec", "drop"}

    def process(self, request: dict[str, Any]) -> dict[str, Any]:
        user_id = request.get("user_id", "anonymous")
        intent = request.get("intent", "")

        # 简单规则：匿名用户禁止危险操作
        if user_id == "anonymous" and intent in self.DANGEROUS_INTENTS:
            raise ToolParamError(f"匿名用户禁止执行危险操作: {intent}")

        # 标记风险等级到 request，供下游 Router 使用
        if intent in self.DANGEROUS_INTENTS:
            request["risk_level"] = 5
            request["needs_approval"] = True
        else:
            request["risk_level"] = request.get("risk_level", 1)
            request["needs_approval"] = request.get("needs_approval", False)

        return request


class AuditLogger(Middleware):
    """审计日志：记录请求开始/结束的关键信息。"""

    name = "audit"

    def process(self, request: dict[str, Any]) -> dict[str, Any]:
        # 脱敏：不记录完整 messages（可能含敏感信息），只记长度
        audit_entry = {
            "trace_id": request.get("trace_id", "unknown"),
            "user_id": request.get("user_id", "anonymous"),
            "intent": request.get("intent", ""),
            "msg_count": len(request.get("messages", [])),
            "risk_level": request.get("risk_level", 1),
        }
        logger.info("Audit start", extra=audit_entry)
        request["_audit_start"] = audit_entry
        return request

    def on_complete(
        self, request: dict[str, Any], response: dict[str, Any] | None, error: Exception | None = None
    ) -> None:
        start_entry = request.get("_audit_start", {})
        start_entry["status"] = "error" if error else "success"
        if error:
            start_entry["error_type"] = type(error).__name__
            start_entry["error_msg"] = str(error)[:200]
        logger.info("Audit end", extra=start_entry)


# ------------------------------------------------------------------------------
# 默认中间件链工厂
# ------------------------------------------------------------------------------


def build_default_chain() -> MiddlewareChain:
    """构建 OpenClaw 默认中间件链。

    顺序：trace_id → 参数校验 → 权限检查 → 审计日志
    """
    return MiddlewareChain().add(TraceIdInjector()).add(ParamValidator()).add(PermissionCheck()).add(AuditLogger())


# 全局默认实例（懒加载复用）
_default_chain: MiddlewareChain | None = None


def get_middleware_chain() -> MiddlewareChain:
    """获取全局默认中间件链。"""
    global _default_chain
    if _default_chain is None:
        _default_chain = build_default_chain()
    return _default_chain


# ------------------------------------------------------------------------------
# 便捷函数：一行代码跑完整中间件 + handler
# ------------------------------------------------------------------------------


def process_request(
    request: dict[str, Any],
    handler: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """使用默认中间件链处理请求并执行 handler。"""
    return get_middleware_chain().run_with_cleanup(request, handler)
