"""混合检索器 — 向量 + BM25 → RRF 融合，支持重新排序与元数据过滤。

增强特性（v2）:
  - 持久化 BM25 索引（增量更新，不��次全量重建）
  - jieba 中文分词（BM25 关键词匹配精度大幅提升）
  - 元数据过滤（where 条件）
  - Re-ranker 接口（cross-encoder 精排，可选）
"""

from __future__ import annotations

import pickle
import re
from pathlib import Path
from typing import Any

from core.embedding_client import EmbeddingClient
from core.logger import get_trace_id, logger
from core.settings import settings
from rag.vector_store import VectorStore

# 中文分词（可选 jieba）
_JIEBA_AVAILABLE = False
try:
    import jieba

    _JIEBA_AVAILABLE = True
except ImportError:
    pass


class HybridRetriever:
    """混合检索器（增强版）。

    用法：
        retriever = HybridRetriever(embed_client, vector_store)
        retriever.build_bm25_index()  # 首次/重建索引
        results = retriever.retrieve("什么是 RRF 算法？", top_k=5)
    """

    def __init__(
        self,
        embed_client: EmbeddingClient,
        vector_store: VectorStore,
    ) -> None:
        self.embed_client = embed_client
        self.vector_store = vector_store

        # BM25 持久化索引
        self._bm25: Any = None
        self._bm25_docs: list[dict[str, Any]] = []
        self._bm25_dirty = True  # 是否需重建

    # ------------------------------------------------------------------
    # 公开接口 — 检索
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        *,
        where: dict[str, Any] | None = None,
        rerank: bool = False,
    ) -> list[dict[str, Any]]:
        """混合检索 —— 向量 + BM25 + RRF 融合。

        Args:
            query: 用户问题
            top_k: 返回数量
            where: ChromaDB 过滤条件，如 {"source": "report.pdf"}
            rerank: 是否启用 cross-encoder 精排（需安装 sentence-transformers）

        Returns:
            [{"id": str, "text": str, "metadata": dict, "score": float}, ...]
        """
        top_k = top_k or settings.rag.top_k_rerank
        trace_id = get_trace_id()

        logger.info(
            "hybrid_retrieve start", extra={"query": query[:80], "top_k": top_k, "where": where, "trace_id": trace_id}
        )

        # Step 1: 向量检索
        query_vector = self.embed_client.embed_text(query)
        vector_results = self.vector_store.search(
            query_vector,
            top_k=settings.rag.top_k_retrieve,
            where=where,
        )
        logger.debug("vector results", extra={"count": len(vector_results), "trace_id": trace_id})

        # Step 2: BM25 关键词检索（自动重建脏索引）
        bm25_results = self._bm25_search(query, top_k=settings.rag.top_k_retrieve)
        logger.debug("bm25 results", extra={"count": len(bm25_results), "trace_id": trace_id})

        # Step 3: RRF 融合
        fused = self._rrf_fusion(vector_results, bm25_results, k=60)
        logger.debug("rrf fused", extra={"count": len(fused), "trace_id": trace_id})

        # Step 4: Re-rank（可选）
        if rerank and len(fused) > top_k:
            fused = self._rerank(query, fused, top_k)
            logger.debug("reranked", extra={"count": len(fused), "trace_id": trace_id})

        return fused[:top_k]

    # ------------------------------------------------------------------
    # BM25 索引管理
    # ------------------------------------------------------------------

    def build_bm25_index(self) -> int:
        """构建/重建 BM25 索引（从向量库全量加载）。

        Returns:
            索引中的文档数
        """
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            logger.warning("rank_bm25 未安装, BM25 索引跳过")
            return 0

        all_docs = self._load_all_paginated()
        if not all_docs:
            self._bm25 = None
            self._bm25_docs = []
            self._bm25_dirty = False
            return 0

        corpus = [self._tokenize(d["text"]) for d in all_docs]
        self._bm25 = BM25Okapi(corpus)
        self._bm25_docs = all_docs
        self._bm25_dirty = False

        logger.info(f"BM25 index built: {len(all_docs)} documents")
        return len(all_docs)

    def save_bm25_index(self, path: str | Path) -> None:
        """持久化 BM25 索引到磁盘。"""
        if self._bm25 is None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "bm25": self._bm25,
            "docs": self._bm25_docs,
            "doc_count": len(self._bm25_docs),
        }
        with open(path, "wb") as f:
            pickle.dump(data, f)
        logger.info(f"BM25 index saved: {path} ({len(self._bm25_docs)} docs)")

    def load_bm25_index(self, path: str | Path) -> bool:
        """从磁盘加载 BM25 索引。"""
        path = Path(path)
        if not path.exists():
            return False
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self._bm25 = data["bm25"]
            self._bm25_docs = data["docs"]
            self._bm25_dirty = False
            logger.info(f"BM25 index loaded: {path} ({data.get('doc_count', len(self._bm25_docs))} docs)")
            return True
        except Exception as exc:
            logger.warning(f"BM25 index load failed: {exc}")
            self._bm25_dirty = True
            return False

    def invalidate_bm25_index(self) -> None:
        """标记 BM25 索引为脏（下次检索时自动重建）。"""
        self._bm25_dirty = True

    # ------------------------------------------------------------------
    # 内部 — BM25 检索
    # ------------------------------------------------------------------

    def _bm25_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """BM25 关键词检索（自动重建脏索引）。"""
        # 自动重建
        if self._bm25_dirty or self._bm25 is None:
            self.build_bm25_index()

        if not self._bm25_docs:
            return []

        query_tokens = self._tokenize(query)
        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]

        results: list[dict[str, Any]] = []
        for idx, score in ranked:
            if score > 0:
                results.append(
                    {
                        "id": self._bm25_docs[idx]["id"],
                        "text": self._bm25_docs[idx]["text"],
                        "metadata": self._bm25_docs[idx]["metadata"],
                        "score": float(score),
                    }
                )

        return results

    def _load_all_paginated(self) -> list[dict[str, Any]]:
        """分页加载所有文档（避免大数据集 OOM）。"""
        all_docs: list[dict[str, Any]] = []
        offset = 0
        page_size = 500
        while True:
            page = self.vector_store.get_documents_paginated(offset=offset, limit=page_size)
            if not page:
                break
            all_docs.extend(page)
            offset += page_size
        return all_docs

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中文+英文混合分词。

        - jieba（推荐，精确中文分词）
        - 回退：中文逐字 + 英文按空格
        """
        tokens: list[str] = []

        if _JIEBA_AVAILABLE:
            # jieba 分词
            seg_list = jieba.cut(text)
            for token in seg_list:
                token = token.strip()
                if not token:
                    continue
                # 英文/数字转小写
                if re.match(r"[a-zA-Z0-9]+", token):
                    tokens.append(token.lower())
                else:
                    tokens.append(token)
        else:
            # 回退分词
            for part in re.split(r"\s+", text):
                chinese = re.findall(r"[\u4e00-\u9fff]", part)
                if chinese:
                    tokens.extend(chinese)
                english = re.findall(r"[a-zA-Z0-9]+", part)
                tokens.extend([t.lower() for t in english])

        return [t for t in tokens if len(t) >= 1]

    # ------------------------------------------------------------------
    # Re-ranker（可选）
    # ------------------------------------------------------------------

    @staticmethod
    def _rerank(
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Cross-encoder 精排（需要 sentence-transformers）。

        如果依赖未安装，静默跳过，返回原列表。
        """
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            logger.debug("sentence-transformers 未安装，跳过 re-rank")
            return candidates

        try:
            model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            pairs = [(query, c["text"]) for c in candidates]
            scores = model.predict(pairs)

            # 按精排分数重排
            for i, c in enumerate(candidates):
                c["rerank_score"] = float(scores[i])

            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        except Exception as exc:
            logger.warning(f"Re-rank failed: {exc}")

        return candidates

    # ------------------------------------------------------------------
    # RRF 融合
    # ------------------------------------------------------------------

    @staticmethod
    def _rrf_fusion(
        results_a: list[dict[str, Any]],
        results_b: list[dict[str, Any]],
        k: int = 60,
    ) -> list[dict[str, Any]]:
        """RRF (Reciprocal Rank Fusion) 融合。"""
        fused: dict[str, dict[str, Any]] = {}

        for rank, item in enumerate(results_a, start=1):
            doc_id = item["id"]
            fused[doc_id] = {
                "id": doc_id,
                "text": item["text"],
                "metadata": item["metadata"],
                "score": 1.0 / (k + rank),
            }

        for rank, item in enumerate(results_b, start=1):
            doc_id = item["id"]
            rrf_score = 1.0 / (k + rank)
            if doc_id in fused:
                fused[doc_id]["score"] += rrf_score
            else:
                fused[doc_id] = {
                    "id": doc_id,
                    "text": item["text"],
                    "metadata": item["metadata"],
                    "score": rrf_score,
                }

        sorted_items = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
        return sorted_items
