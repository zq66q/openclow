"""API 数据模型 — Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import Any, Optional


try:
    from pydantic import BaseModel, Field
except ImportError:

    class BaseModel:  # type: ignore[no-redef]
        def __init__(self, **data: Any) -> None:
            for k, v in data.items():
                setattr(self, k, v)

    def Field(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        return None


# ── 通用 ──


class HealthResponse(BaseModel):
    status: str
    version: str = "0.1.0"
    uptime_seconds: float = 0
    components: dict[str, str] = Field(default_factory=dict)


# ── 聊天 ──


class ChatRequest(BaseModel):
    query: str = Field(..., description="用户消息")
    scenario: str = Field(default="general_assistant", description="场景类型")
    session_id: Optional[str] = Field(default=None, description="会话 ID")
    stream: bool = Field(default=False, description="是否流式")


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    scenario: str
    elapsed_ms: float = 0


# ── 会话 ──


class SessionCreate(BaseModel):
    user_id: str = Field(default="default", description="用户 ID")
    title: str = Field(default="新会话", description="会话标题")


class SessionInfo(BaseModel):
    session_id: str
    user_id: str
    title: str
    status: str
    message_count: int
    created_at: str = ""
    updated_at: str = ""


# ── RAG 知识库 ──


class RAGIngestRequest(BaseModel):
    text: str = Field(..., description="要入库的文本内容")
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="", description="来源标识")


class RAGIngestResponse(BaseModel):
    status: str
    chunks: int = 0
    tokens: int = 0
    source: str = ""


class RAGStatusResponse(BaseModel):
    available: bool
    document_count: int = 0
    sources: list[dict[str, Any]] = Field(default_factory=list)


class RAGClearResponse(BaseModel):
    status: str
    deleted_count: int = 0


class RAGDeleteSourceResponse(BaseModel):
    status: str
    source: str
    deleted_chunks: int = 0


# ── 工作流 ──


class WorkflowRunRequest(BaseModel):
    workflow_name: str = Field(..., description="工作流名称: data_analysis / code_review / rag_qa")
    query: str = Field(..., description="用户查询/输入")


class WorkflowRunResponse(BaseModel):
    status: str
    workflow_name: str
    result: Any = None
    elapsed_ms: float = 0


# ── 记忆搜索 ──


class MemorySearchResponse(BaseModel):
    query: str
    results: list[Any] = Field(default_factory=list)
