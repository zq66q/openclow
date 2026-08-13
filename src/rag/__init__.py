"""RAG 检索增强生成层 — 生产级 v2。

模块职责：
- document_parser: 多格式文档解析（PDF / Word / Excel / PPT / HTML / CSV / 代码）
- chunker:       多模式文档切片（文本 / Markdown / 代码）
- vector_store:   向量存储（ChromaDB）+ 备份恢复
- retriever:      混合检索（向量 + BM25 → RRF 融合）+ re-rank
- pipeline:       编排流水线（入库 / 查询 / 增量更新 / source 管理）
"""

from rag.chunker import Chunk, CodeChunker, MarkdownChunker, TextChunker
from rag.document_parser import (
    parse_csv,
    parse_docx,
    parse_excel,
    parse_file,
    parse_html,
    parse_pdf,
    parse_pptx,
    parse_text,
)
from rag.pipeline import RAGPipeline
from rag.retriever import HybridRetriever
from rag.vector_store import VectorStore

__all__ = [
    # 流水线
    "RAGPipeline",
    # 检索引擎
    "HybridRetriever",
    "VectorStore",
    # 分块器
    "TextChunker",
    "MarkdownChunker",
    "CodeChunker",
    "Chunk",
    # 文档解析
    "parse_file",
    "parse_pdf",
    "parse_docx",
    "parse_pptx",
    "parse_excel",
    "parse_html",
    "parse_csv",
    "parse_text",
]
