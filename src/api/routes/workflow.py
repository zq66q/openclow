"""工作流执行路由。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException

from api.models import WorkflowRunRequest, WorkflowRunResponse

router = APIRouter(prefix="/workflow", tags=["工作流"])

_VALID_WORKFLOWS = frozenset({"data_analysis", "code_review", "rag_qa"})


def _get_svc() -> Any:
    from api.server import get_facade
    return get_facade()


@router.post("/run", response_model=WorkflowRunResponse)
async def run_workflow(req: WorkflowRunRequest) -> dict[str, Any]:
    """执行预设工作流。

    支持的工作流:
        - data_analysis: 理解需求 → 数据计算 → 汇总
        - code_review:    读取代码 → 审查代码
        - rag_qa:         检索知识库 → 基于上下文回答
    """
    if req.workflow_name not in _VALID_WORKFLOWS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown workflow: {req.workflow_name}. Valid: {sorted(_VALID_WORKFLOWS)}",
        )

    svc = _get_svc()
    t0 = time.time()
    try:
        result = svc.run_workflow(
            workflow_name=req.workflow_name,
            query=req.query,
        )
        elapsed_ms = (time.time() - t0) * 1000

        # 规范化返回值
        output = result
        if hasattr(result, "__dict__"):
            output = str(result)
        elif isinstance(result, dict):
            output = result

        return {
            "status": "ok",
            "workflow_name": req.workflow_name,
            "result": output,
            "elapsed_ms": round(elapsed_ms, 1),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
