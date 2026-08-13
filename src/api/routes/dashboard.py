"""仪表盘路由 — 可用 Agent 列表、场景列表、记忆搜索。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from api.models import MemorySearchResponse

router = APIRouter(tags=["仪表盘"])


def _get_svc() -> Any:
    from api.server import get_facade
    return get_facade()


@router.get("/agents")
async def list_agents() -> dict[str, list[str]]:
    """列出所有可用的 Agent 预设。"""
    return {
        "agents": [
            "general",
            "rag_qa",
            "tool_calling",
            "data_analysis",
            "code_review",
            "multi_agent",
            "researcher",
            "master",
        ]
    }


@router.get("/scenarios")
async def list_scenarios() -> dict[str, list[str]]:
    """列出所有可用的对话场景。"""
    return {
        "scenarios": [
            "general_assistant",
            "rag_customer_service",
            "data_analyst",
            "code_reviewer",
            "multi_agent",
            "master_orchestrator",
        ]
    }


@router.post("/memory/search", response_model=MemorySearchResponse)
async def memory_search(query: str = "", user_id: str = "default") -> dict[str, Any]:
    """搜索用户的长期记忆。"""
    svc = _get_svc()
    mm = svc.memory_manager
    if mm is None:
        raise HTTPException(status_code=503, detail="Memory manager not available")
    try:
        results = mm.search(query, user_id=user_id)
        return {"query": query, "results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
