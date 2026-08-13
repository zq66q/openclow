"""会话管理路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from api.models import SessionCreate, SessionInfo

router = APIRouter(prefix="/sessions", tags=["会话"])


def _get_sm() -> Any:
    from api.server import get_facade
    svc = get_facade()
    sm = svc.session_manager
    if sm is None:
        raise HTTPException(status_code=503, detail="Session manager not available")
    return sm


def _to_session_info(s: Any) -> dict[str, Any]:
    return {
        "session_id": s.session_id,
        "user_id": getattr(s, "user_id", ""),
        "title": getattr(s, "title", ""),
        "status": getattr(s, "status", "active"),
        "message_count": len(getattr(s, "messages", [])),
        "created_at": str(getattr(s, "created_at", "")),
        "updated_at": str(getattr(s, "updated_at", "")),
    }


@router.get("", response_model=list[SessionInfo])
async def list_sessions(user_id: str = "default") -> list[dict[str, Any]]:
    """列出指定用户的所有会话。"""
    sm = _get_sm()
    sessions = sm.list_by_user(user_id)
    return [_to_session_info(s) for s in sessions]


@router.post("", response_model=SessionInfo)
async def create_session(req: SessionCreate) -> dict[str, Any]:
    """创建新会话。"""
    sm = _get_sm()
    s = sm.create(user_id=req.user_id, title=req.title)
    return _to_session_info(s)


@router.get("/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str) -> dict[str, Any]:
    """获取指定会话详情。"""
    sm = _get_sm()
    s = sm.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _to_session_info(s)


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    """删除指定会话。"""
    sm = _get_sm()
    sm.delete(session_id)
    return {"status": "deleted", "session_id": session_id}
