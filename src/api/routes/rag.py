"""RAG 知识库路由 — 文件上传、文本入库、状态查询、清理。"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api.models import (
    RAGClearResponse,
    RAGDeleteSourceResponse,
    RAGIngestRequest,
    RAGIngestResponse,
    RAGStatusResponse,
)

router = APIRouter(prefix="/rag", tags=["RAG 知识库"])


def _get_pipeline() -> Any:
    """获取 RAG pipeline，不可用时抛 503。"""
    from api.server import get_facade

    svc = get_facade()
    pipeline = svc.rag_pipeline
    if pipeline is None:
        raise HTTPException(status_code=503, detail="RAG pipeline not available (check EMBEDDING_API_KEY)")
    return pipeline


# ── 文本入库 ──


@router.post("/ingest", response_model=RAGIngestResponse)
async def rag_ingest(req: RAGIngestRequest) -> dict[str, Any]:
    """将纯文本入库到知识库。

    支持去重：相同 source 会先删除旧 chunk 再入库新内容。
    """
    pipeline = _get_pipeline()
    try:
        result = pipeline.ingest_text(
            req.text,
            metadata=req.metadata,
            source=req.source,
        )
        return {
            "status": "ok",
            "chunks": result.get("chunks", 0),
            "tokens": result.get("tokens", 0),
            "source": result.get("source", req.source),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── 文件上传入库 ──


@router.post("/upload", response_model=RAGIngestResponse)
async def rag_upload(
    file: UploadFile = File(..., description="要上传的文档 (PDF/DOCX/PPTX/TXT/MD/CSV/JSON/HTML)"),
    source: str = Form(default="", description="来源标识（留空则使用文件名）"),
) -> dict[str, Any]:
    """上传文档文件到知识库。

    支持格式: txt, md, pdf, csv, json, docx, doc, pptx, xlsx, html

    文件会被解析、分块、向量化后存入 ChromaDB。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # 检查文件大小（防止过大文件）
    content = await file.read()
    max_size = 50 * 1024 * 1024  # 50MB
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail=f"File too large (max {max_size // 1024 // 1024}MB)")

    pipeline = _get_pipeline()

    # 写入临时文件
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = pipeline.ingest_file(tmp_path)
        chunks = result.get("chunks", 0)
        if chunks == 0:
            raise HTTPException(
                status_code=422,
                detail=f"File '{file.filename}' processed but produced no chunks (empty or unsupported format?)",
            )
        return {
            "status": "ok",
            "chunks": chunks,
            "tokens": result.get("tokens", 0),
            "source": source or result.get("source", file.filename),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ingest failed: {exc}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ── 知识库状态 ──


@router.get("/status", response_model=RAGStatusResponse)
async def rag_status() -> dict[str, Any]:
    """查询知识库状态：文档总数、已入库来源列表。"""
    from api.server import get_facade

    svc = get_facade()
    pipeline = svc.rag_pipeline
    if pipeline is None:
        return {"available": False, "document_count": 0, "sources": []}

    return {
        "available": True,
        "document_count": pipeline.document_count,
        "sources": pipeline.list_sources(),
    }


# ── 清空知识库 ──


@router.post("/clear", response_model=RAGClearResponse)
async def rag_clear() -> dict[str, Any]:
    """清空知识库中的所有文档。"""
    pipeline = _get_pipeline()
    before = pipeline.document_count
    try:
        pipeline.clear_all()
        after = pipeline.document_count
        return {"status": "ok", "deleted_count": before - after}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── 按来源删除 ──


@router.delete("/source/{source:path}", response_model=RAGDeleteSourceResponse)
async def rag_delete_source(source: str) -> dict[str, Any]:
    """按来源标识删除知识库中的文档。

    示例: DELETE /rag/source/manual  删除所有手动入库的文本
    """
    pipeline = _get_pipeline()
    try:
        deleted = pipeline.remove_source(source)
        return {"status": "ok", "source": source, "deleted_chunks": deleted}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
