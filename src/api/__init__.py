"""API 层 — FastAPI REST + SSE + WebSocket 流式服务。

提供:
    - RESTful 端点: /chat, /sessions, /rag/ingest, /memory/search
    - SSE 流式: /chat/stream
    - WebSocket 双向流式: /chat/ws
    - 健康检查: /health
    - 认证集成: APIKey + JWT 双轨
"""

from __future__ import annotations

from api.server import create_app, start

__all__ = ["create_app", "start"]
