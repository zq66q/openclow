# FastAPI 服务器主入口

"""FastAPI 后端服务 — 提供 RESTful API + SSE + WebSocket 流式接口。

端点:
    GET  /health          — 健康检查
    POST /chat            — 同步对话
    POST /chat/stream     — SSE 流式对话（Master Agent 编排）
    WS   /chat/ws         — WebSocket 流式对话（双向实时）
    GET  /sessions        — 列出会话
    POST /sessions        — 创建会话
    GET  /sessions/{id}   — 获取会话
    DELETE /sessions/{id} — 删除会话
    POST /rag/ingest      — 文本入库
    POST /rag/upload      — 文件上传入库
    GET  /rag/status      — 知识库状态
    POST /rag/clear       — 清空知识库
    DELETE /rag/source/{s}— 按来源删除文档
    POST /workflow/run    — 执行工作流
    POST /memory/search   — 记忆搜索
    GET  /agents          — 列出可用 Agent 预设
    GET  /scenarios       — 列出可用场景
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from typing import Any

from core.logger import logger

# 加载 .env 文件到环境变量，确保 os.getenv 能读到配置
try:
    from dotenv import load_dotenv

    load_dotenv(".env")
except Exception:
    pass

# ── 可选依赖检测 & 优雅降级 ──

_HAS_FASTAPI = False
_HAS_UVICORN = False

try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware

    _HAS_FASTAPI = True
except ImportError:
    FastAPI = object  # type: ignore[misc,assignment]

try:
    import uvicorn

    _HAS_UVICORN = True
except ImportError:
    uvicorn = None  # type: ignore[assignment]


# ── 全局状态 ──

_app: Any = None
_started_at: float = 0
_facade: Any = None


def get_facade() -> Any:
    """获取 ServiceFacade 实例，自动启动。"""
    global _facade
    if _facade is None:
        from business.service_facade import ServiceConfig, ServiceFacade

        config = ServiceConfig.from_env()
        if not config.llm_api_key:
            # fallback: 尝试读取标准变量名
            config.llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")

        _facade = ServiceFacade(config)
        _facade.start()
    return _facade


# ── 应用构建 ──


def create_app(facade: Any = None, title: str = "OpenClaw API") -> Any:
    """创建并配置 FastAPI 应用。

    Args:
        facade: 可选的 ServiceFacade 实例
        title: API 标题
    """
    global _app, _started_at, _facade

    if not _HAS_FASTAPI:
        logger.warning("FastAPI not installed — returning stub app")
        return _StubApp()

    if facade is not None:
        _facade = facade

    _app = FastAPI(
        title=title,
        description="OpenClaw 企业级多 Agent 业务自动化平台 API",
        version="0.1.0",
    )

    # ── 认证中间件 ──
    _auth_mode = os.getenv("OPENCLAW_AUTH_MODE", "apikey").strip().lower()
    if _auth_mode != "none":
        try:
            from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

            from auth.middleware import auth_guard

            class AuthMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Any:
                    # 白名单: health 和 docs 端点免认证
                    if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
                        return await call_next(request)
                    try:
                        auth_guard(request)
                    except Exception:
                        from starlette.responses import JSONResponse

                        return JSONResponse(
                            status_code=401,
                            content={"detail": "Authentication required"},
                            headers={"WWW-Authenticate": "X-API-Key, Bearer"},
                        )
                    return await call_next(request)

            _app.add_middleware(AuthMiddleware)
            logger.info(f"Authentication enabled (mode={_auth_mode})")
        except Exception as exc:
            logger.warning(f"Auth middleware setup failed: {exc}")

    # CORS
    _app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _started_at = time.time()

    # ── 注册路由模块 ──

    from api.routes.chat import router as chat_router
    from api.routes.dashboard import router as dashboard_router
    from api.routes.rag import router as rag_router
    from api.routes.sessions import router as sessions_router
    from api.routes.workflow import router as workflow_router

    _app.include_router(chat_router)
    _app.include_router(sessions_router)
    _app.include_router(rag_router)
    _app.include_router(workflow_router)
    _app.include_router(dashboard_router)

    # ── 健康检查（保留内联，简单且需要 startup 时间） ──

    from api.models import HealthResponse

    @_app.get("/health", response_model=HealthResponse)
    async def health() -> dict[str, Any]:
        svc = get_facade()
        report = svc.health_check()
        return {
            "status": report.status.value if hasattr(report.status, "value") else str(report.status),
            "version": "0.1.0",
            "uptime_seconds": time.time() - _started_at,
            "components": report.components,
        }

    # ── WebSocket 流式对话 ──

    _HEARTBEAT_INTERVAL = 30.0  # 秒
    _WS_MAX_MESSAGE_SIZE = 65536  # 64KB

    @_app.websocket("/chat/ws")
    async def ws_chat(websocket: WebSocket) -> None:
        """WebSocket 流式对话端点。

        消息格式 (客户端 → 服务端):
            {"type": "chat", "query": "你好", "scenario": "general_assistant", "session_id": "optional"}
            {"type": "ping"}

        消息格式 (服务端 → 客户端):
            {"type": "chunk", "content": "部分文本", "index": 0}
            {"type": "done", "session_id": "...", "elapsed_ms": 123.4, "total_chunks": 5}
            {"type": "error", "message": "错误描述"}
            {"type": "pong"}
            {"type": "heartbeat"}
            {"type": "connected", "scenarios": [...]}
        """
        await websocket.accept()

        # 认证检查
        auth_mode = os.getenv("OPENCLAW_AUTH_MODE", "apikey").strip().lower()
        if auth_mode != "none":
            try:
                from auth.middleware import APIKeyAuth, JWTAuth

                api_auth = APIKeyAuth()
                jwt_auth = JWTAuth()
                headers = dict(websocket.headers)

                if auth_mode == "apikey":
                    if not api_auth.verify(headers):
                        await websocket.send_json({"type": "error", "message": "Authentication required"})
                        await websocket.close(code=4001, reason="Unauthorized")
                        return
                elif auth_mode == "jwt":
                    if not jwt_auth.verify(headers):
                        await websocket.send_json({"type": "error", "message": "JWT authentication required"})
                        await websocket.close(code=4001, reason="Unauthorized")
                        return
                elif auth_mode == "both" and not (api_auth.verify(headers) or jwt_auth.verify(headers)):
                    await websocket.send_json({"type": "error", "message": "Authentication required"})
                    await websocket.close(code=4001, reason="Unauthorized")
                    return
            except Exception as exc:
                logger.warning(f"WS auth error: {exc}")
                await websocket.send_json({"type": "error", "message": f"Auth error: {exc}"})
                await websocket.close(code=4001, reason="Auth error")
                return

        # 发送连接确认
        await websocket.send_json(
            {
                "type": "connected",
                "scenarios": [
                    "general_assistant",
                    "rag_customer_service",
                    "data_analyst",
                    "code_reviewer",
                ],
                "auth_mode": auth_mode,
            }
        )

        # 心跳任务
        heartbeat_running = True

        async def heartbeat_loop() -> None:
            while heartbeat_running:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                if heartbeat_running:
                    try:
                        await websocket.send_json({"type": "heartbeat"})
                    except Exception:
                        break

        heartbeat_task = asyncio.create_task(heartbeat_loop())

        try:
            while True:
                raw = await websocket.receive_text()

                if len(raw) > _WS_MAX_MESSAGE_SIZE:
                    await websocket.send_json({"type": "error", "message": "Message too large"})
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue

                if msg_type != "chat":
                    await websocket.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})
                    continue

                query = msg.get("query", "")
                if not query or not query.strip():
                    await websocket.send_json({"type": "error", "message": "Empty query"})
                    continue

                scenario = msg.get("scenario", "general_assistant")
                session_id = msg.get("session_id")

                t0 = time.time()
                svc = get_facade()

                try:
                    app = svc.create_scenario(scenario)
                    llm = getattr(app.agent, "_llm", None) or getattr(app.agent, "llm_client", None)
                    can_stream = llm is not None and hasattr(llm, "astream_chat")

                    if can_stream:
                        session = None
                        if session_id:
                            session = app.session_manager.get(session_id)
                        if session is None:
                            session = app.session_manager.create(user_id="default", title=query[:30])

                        app.session_manager.append_message(session.session_id, "user", query)

                        history = app.session_manager.get_messages(session.session_id, last_n=20)
                        messages: list[dict[str, str]] = []
                        for m in history:
                            if m.get("role") in ("user", "assistant", "system"):
                                messages.append({"role": m["role"], "content": m["content"]})

                        chunk_index = 0
                        full_answer_parts: list[str] = []

                        try:
                            async for token in llm.astream_chat(messages):
                                full_answer_parts.append(token)
                                await websocket.send_json(
                                    {
                                        "type": "chunk",
                                        "content": token,
                                        "index": chunk_index,
                                    }
                                )
                                chunk_index += 1
                        except NotImplementedError:
                            can_stream = False

                        if can_stream:
                            full_answer = "".join(full_answer_parts)
                            app.session_manager.append_message(session.session_id, "assistant", full_answer)
                            elapsed_ms = (time.time() - t0) * 1000
                            await websocket.send_json(
                                {
                                    "type": "done",
                                    "session_id": session.session_id,
                                    "elapsed_ms": round(elapsed_ms, 1),
                                    "total_chunks": chunk_index,
                                }
                            )
                            continue

                    # 非流式模式
                    answer = app.chat(query, session_id=session_id)
                    elapsed_ms = (time.time() - t0) * 1000

                    await websocket.send_json(
                        {
                            "type": "chunk",
                            "content": answer,
                            "index": 0,
                        }
                    )
                    await websocket.send_json(
                        {
                            "type": "done",
                            "session_id": session_id or "auto",
                            "elapsed_ms": round(elapsed_ms, 1),
                            "total_chunks": 1,
                        }
                    )

                except Exception as exc:
                    logger.error(f"WebSocket chat error: {exc}")
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": str(exc),
                        }
                    )

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as exc:
            logger.error(f"WebSocket fatal error: {exc}")
        finally:
            heartbeat_running = False
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    return _app


# ── 启动入口 ──


def start(
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
    facade: Any = None,
) -> None:
    """启动 FastAPI 服务。

    Args:
        host: 监听地址
        port: 监听端口
        reload: 是否热重载
        facade: ServiceFacade 实例
    """
    if not _HAS_UVICORN:
        logger.error("uvicorn not installed — cannot start server")
        return

    app = create_app(facade=facade)
    logger.info(f"Starting API server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, reload=reload, log_level="info")


# ── 占位（无 FastAPI 时） ──


class _StubApp:
    """FastAPI 不可用时的占位。"""

    def __init__(self) -> None:
        self.routes: list[Any] = []

    def add_middleware(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get(self, path: str, **kwargs: Any) -> Any:
        return lambda fn: fn

    def post(self, path: str, **kwargs: Any) -> Any:
        return lambda fn: fn

    def delete(self, path: str, **kwargs: Any) -> Any:
        return lambda fn: fn

    def include_router(self, *args: Any, **kwargs: Any) -> None:
        pass
