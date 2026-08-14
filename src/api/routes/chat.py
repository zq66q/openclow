"""聊天路由 — 同步对话 + SSE 流式对话。"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.metrics import StreamCounter
from api.models import ChatRequest, ChatResponse

router = APIRouter(prefix="/chat", tags=["对话"])


def _get_svc() -> Any:
    from api.server import get_facade

    return get_facade()


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest) -> dict[str, Any]:
    """同步对话 — 发送一条消息，返回完整回答。"""
    svc = _get_svc()
    t0 = time.time()
    try:
        answer = svc.chat(
            query=req.query,
            scenario=req.scenario,
            session_id=req.session_id,
        )
        elapsed_ms = (time.time() - t0) * 1000
        return {
            "answer": answer,
            "session_id": req.session_id or "auto",
            "scenario": req.scenario,
            "elapsed_ms": round(elapsed_ms, 1),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE 流式对话 — 使用 Master Agent 编排，逐步推送思考/委派/结果。

    事件类型:
        {"type":"think",      "thought":"...", "tool":"...", "task":"..."}
        {"type":"delegate",   "tool":"...", "task":"..."}
        {"type":"tool_result","tool":"...", "result":"...", "elapsed_ms":123}
        {"type":"waiting"}
        {"type":"done",       "answer":"...", "elapsed_ms":..., "steps":[...]}
        {"type":"error",      "error":"..."}
    """
    svc = _get_svc()

    # 检查是否支持流式
    if not hasattr(svc, "master_agent_chat_stream"):
        raise HTTPException(status_code=501, detail="Streaming not available — master_agent_chat_stream not found")

    # 并发流式连接数限制（OPENCLAW_MAX_STREAMS，默认 64）
    counter = StreamCounter()
    if not counter.try_enter():
        raise HTTPException(status_code=503, detail="Too many concurrent streams, try again later")

    try:
        return StreamingResponse(
            event_generator(counter, svc, req),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception:
        counter.release()
        raise


def event_generator(
    counter: StreamCounter,
    svc: Any,
    req: ChatRequest,
) -> AsyncGenerator[str, None]:
    """SSE 事件生成器。结束（正常或异常）时释放流式槽位。"""

    async def _gen() -> AsyncGenerator[str, None]:
        try:
            for event in svc.master_agent_chat_stream(query=req.query):
                # 映射内部事件类型到 API 事件类型
                evt_type = event.get("type", "")

                if evt_type == "_done":
                    yield f"data: {json.dumps({'type': 'done', 'answer': event['answer'], 'elapsed_ms': event['elapsed_ms'], 'steps': event.get('steps', [])}, ensure_ascii=False)}\n\n"
                    break
                elif evt_type == "_error":
                    yield f"data: {json.dumps({'type': 'error', 'error': event['error']}, ensure_ascii=False)}\n\n"
                    break
                elif evt_type == "think":
                    yield f"data: {json.dumps({'type': 'think', 'thought': event.get('thought', ''), 'tool': event.get('tool', ''), 'task': event.get('task', '')}, ensure_ascii=False)}\n\n"
                elif evt_type == "delegate":
                    yield f"data: {json.dumps({'type': 'delegate', 'tool': event.get('tool', ''), 'task': event.get('task', '')}, ensure_ascii=False)}\n\n"
                elif evt_type == "tool_result":
                    result_text = str(event.get("result", ""))[:500]
                    yield f"data: {json.dumps({'type': 'tool_result', 'tool': event.get('tool', ''), 'result': result_text, 'elapsed_ms': event.get('elapsed_ms', 0)}, ensure_ascii=False)}\n\n"
                elif evt_type == "waiting":
                    yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            counter.release()

    return _gen()
