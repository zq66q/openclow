#RAG 流水线，一站式入库/查询

"""RAG 编排流水线 — 一站式入库/查询接口（增强版 v2）。

增强特性：
  - 多格式文档解析（PDF/Word/Excel/PPT/HTML/CSV/Markdown）
  - 入库去重（相同 source 的文档自动覆盖旧 chunk）
  - 增量更新（单文档更新，不影响其他文档）
  - source 列表查询（知道知识库里有哪些文档）
  - 批量入库带进度
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from core.embedding_client import EmbeddingClient
from core.logger import get_trace_id, logger
from core.settings import settings
from rag.chunker import CodeChunker, MarkdownChunker, TextChunker
from rag.retriever import HybridRetriever
from rag.vector_store import VectorStore


class RAGPipeline:
    """RAG 检索增强生成流水线（增强版）。

    用法：
        pipeline = RAGPipeline()
        # 入库 PDF
        pipeline.ingest_file("docs/report.pdf")
        # 入库整个目录
        pipeline.ingest_directory("docs/", recursive=True)
        # 查询
        results = pipeline.query("报告里提到了哪些关键指标？", top_k=5)
        # 查看知识库
        sources = pipeline.list_sources()
    """

    def __init__(
        self,
        collection_name: str = "openclaw_kb",
        embed_client: EmbeddingClient | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.embed_client = embed_client or EmbeddingClient()
        self.vector_store = vector_store or VectorStore(collection_name=collection_name)
        self.retriever = HybridRetriever(self.embed_client, self.vector_store)
        self.chunker = TextChunker()
        self._collection_name = collection_name

        # 内容指纹缓存（用于去重）
        self._fingerprints: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 入库 — 文本
    # ------------------------------------------------------------------

    def ingest_text(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        *,
        source: str = "",
        chunker_type: str = "text",
        dedup: bool = True,
    ) -> dict[str, Any]:
        """入库纯文本。

        Args:
            text: 文本内容
            metadata: 附加元数据
            source: 来源标识（文件名/URL）。用于去重和增量更新
            chunker_type: 分块器类型 "text" | "markdown" | "code"
            dedup: 是否去重（相同 source 删除旧 chunk 再入新）

        Returns:
            {"chunks": int, "tokens": int, "source": str}
        """
        trace_id = get_trace_id()
        base_meta = dict(metadata or {})

        if source:
            base_meta["source"] = source
            # 去重/增量更新
            if dedup:
                fingerprint = self._compute_fingerprint(text)
                old_fp = self._fingerprints.get(source)
                if old_fp == fingerprint:
                    logger.info("ingest skipped: content unchanged", extra={"source": source, "trace_id": trace_id})
                    return {"chunks": 0, "tokens": 0, "source": source, "skipped": True}
                # 删除旧 chunk
                deleted = self.vector_store.delete_by_metadata({"source": source})
                if deleted > 0:
                    logger.info(
                        "ingest: removed old chunks", 
                        extra={"source": source, "deleted": deleted, "trace_id": trace_id}
                    )
                self._fingerprints[source] = fingerprint
                # BM25 索引变脏
                self.retriever.invalidate_bm25_index()

        # 选择分块器
        if chunker_type == "markdown":
            chunker = MarkdownChunker()
        elif chunker_type == "code":
            chunker = CodeChunker()
        else:
            chunker = self.chunker

        # 1. 切片
        chunks = chunker.split(text, metadata=base_meta)
        if not chunks:
            logger.warning("ingest: no chunks produced", extra={"trace_id": trace_id})
            return {"chunks": 0, "tokens": 0, "source": source}

        logger.info("ingest chunking", extra={"chunks": len(chunks), "trace_id": trace_id})

        # 2. 向量化
        texts = [c.content for c in chunks]
        vectors, embed_stats = self.embed_client.embed_batch(texts)
        logger.info("ingest embedding", extra=embed_stats | {"trace_id": trace_id})

        # 3. 入库
        metadatas = [c.metadata for c in chunks]
        self.vector_store.add(texts=texts, vectors=vectors, metadatas=metadatas)

        logger.info(
            "ingest complete",
            extra={"total_chunks": len(chunks), "tokens": embed_stats["total_tokens"], "source": source, "trace_id": trace_id},
        )
        return {
            "chunks": len(chunks),
            "tokens": embed_stats["total_tokens"],
            "source": source,
        }

    # ------------------------------------------------------------------
    # 入库 — 文件/目录
    # ------------------------------------------------------------------

    def ingest_file(
        self,
        file_path: str | Path,
        *,
        dedup: bool = True,
    ) -> dict[str, Any]:
        """入库单个文件（自动检测类型并解析）。

        支持格式: PDF, DOCX, PPTX, XLSX, HTML, CSV, MD, TXT, 代码文件
        """
        from rag.document_parser import parse_file

        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        logger.info("ingest_file start", extra={"path": str(file_path)})

        # 解析文档
        text, file_meta = parse_file(file_path)

        # 选择分块器
        suffix = file_path.suffix.lower()
        if suffix in (".md", ".markdown"):
            chunker_type = "markdown"
        elif suffix in (".py", ".js", ".ts", ".java", ".go", ".rs", ".cpp", ".c", ".h"):
            chunker_type = "code"
        else:
            chunker_type = "text"

        return self.ingest_text(
            text,
            metadata=file_meta,
            source=file_path.name,
            chunker_type=chunker_type,
            dedup=dedup,
        )

    def ingest_directory(
        self,
        dir_path: str | Path,
        *,
        recursive: bool = True,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """入库整个目录。

        Args:
            dir_path: 目录路径
            recursive: 是否递归子目录
            progress_callback: 进度回调 (current_file, index, total)

        Returns:
            {"total_files": int, "total_chunks": int, "errors": int}
        """
        dir_path = Path(dir_path)
        if not dir_path.is_dir():
            raise ValueError(f"不是目录: {dir_path}")

        # 收集文件
        if recursive:
            all_files = list(dir_path.rglob("*"))
        else:
            all_files = list(dir_path.glob("*"))

        # 过滤：只保留支持的文件类型
        supported = {
            ".pdf", ".docx", ".doc", ".pptx", ".ppt",
            ".xlsx", ".xls", ".xlsm", ".csv", ".tsv",
            ".html", ".htm", ".md", ".markdown", ".txt",
            ".log", ".json", ".xml", ".yaml", ".yml",
            ".py", ".js", ".ts", ".java", ".go", ".rs",
            ".cpp", ".c", ".h",
        }
        files = [f for f in all_files if f.is_file() and f.suffix.lower() in supported]

        total_chunks = 0
        errors = 0

        for idx, file_path in enumerate(files):
            if progress_callback:
                progress_callback(str(file_path), idx + 1, len(files))

            try:
                result = self.ingest_file(file_path)
                total_chunks += result.get("chunks", 0)
            except Exception as exc:
                logger.error(f"ingest_directory: 入库失败 {file_path}: {exc}")
                errors += 1

        logger.info(
            "ingest_directory complete",
            extra={"files": len(files), "chunks": total_chunks, "errors": errors},
        )
        return {
            "total_files": len(files),
            "total_chunks": total_chunks,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query(
        self,
        question: str,
        top_k: int | None = None,
        *,
        where: dict[str, Any] | None = None,
        rerank: bool = False,
    ) -> list[dict[str, Any]]:
        """检索与问题最相关的 Top-K 片段。

        Args:
            question: 用户问题
            top_k: 返回数量
            where: 元数据过滤条件
            rerank: 是否启用 cross-encoder 精排

        Returns:
            [{"id": str, "text": str, "metadata": dict, "score": float}, ...]
        """
        trace_id = get_trace_id()
        logger.info("rag query start", extra={"question": question[:100], "top_k": top_k, "trace_id": trace_id})

        results = self.retriever.retrieve(question, top_k=top_k, where=where, rerank=rerank)

        logger.info("rag query done", extra={"found": len(results), "trace_id": trace_id})
        return results

    def query_with_context(
        self,
        question: str,
        top_k: int | None = None,
        *,
        where: dict[str, Any] | None = None,
    ) -> str:
        """检索并拼接为 LLM 上下文。

        Returns:
            可直接插入 prompt 的格式化上下文字符串
        """
        results = self.query(question, top_k=top_k, where=where)
        if not results:
            return ""

        parts: list[str] = []
        for i, item in enumerate(results, 1):
            md = item.get("metadata", {})
            source = md.get("source", "unknown")
            heading = md.get("heading_path", "")
            label = f"{source}" + (f" > {heading}" if heading else "")
            parts.append(f"[{i}] (来源: {label}, 相关度: {item['score']:.4f})\n{item['text']}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    @property
    def document_count(self) -> int:
        """向量库中 chunk 总数。"""
        return self.vector_store.count()

    def list_sources(self) -> list[dict[str, Any]]:
        """列出知识库中所有不同的来源文档。"""
        trace_id = get_trace_id()
        # 分页获取所有文档，按 source 聚合
        source_map: dict[str, dict] = {}
        offset = 0
        page_size = 500

        while True:
            page = self.vector_store.get_documents_paginated(offset=offset, limit=page_size)
            if not page:
                break
            for doc in page:
                source = doc["metadata"].get("source", "unknown")
                if source not in source_map:
                    source_map[source] = {"source": source, "chunks": 0, "type": doc["metadata"].get("type", "")}
                source_map[source]["chunks"] += 1
            offset += page_size

        logger.info("list_sources", extra={"sources": len(source_map), "trace_id": trace_id})
        return sorted(source_map.values(), key=lambda x: x["chunks"], reverse=True)

    def remove_source(self, source: str) -> int:
        """按 source 名称删除对应文档的所有 chunk。

        Returns:
            删除的 chunk 数量
        """
        deleted = self.vector_store.delete_by_metadata({"source": source})
        self.retriever.invalidate_bm25_index()
        self._fingerprints.pop(source, None)
        logger.info(f"remove_source: {source}, deleted {deleted} chunks")
        return deleted

    def clear_all(self) -> None:
        """清空整个知识库。"""
        self.vector_store.delete_collection()
        self.retriever.invalidate_bm25_index()
        self._fingerprints.clear()
        logger.warning("知识库已清空")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_fingerprint(text: str) -> str:
        """计算文本内容指纹（用于去重）。"""
        # 取前 2000 字符 + 后 2000 字符的 MD5
        sample = text[:2000] + text[-2000:]
        return hashlib.md5(sample.encode("utf-8")).hexdigest()
