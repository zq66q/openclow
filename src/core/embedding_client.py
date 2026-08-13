#这是文本向量化（Embedding 向量）专用工具类，把文字变成一串数字向量（用来知识库相似度对比），自带两大省钱优化：
#缓存：一模一样的文本不用重复调用大模型接口，直接读内存；
#自动分批：一次性传入很多文字时，自动切割成小批次调用接口，防止超过模型单次最大条数限制。

"""Embedding 客户端 — 文本向量化能力（内部共用模块）。

基于 OpenAI 兼容 API，支持批处理 + 结果缓存。
"""

from __future__ import annotations

import hashlib
from typing import Any

from core.logger import get_trace_id, logger
from core.settings import settings


class EmbeddingClient:
    """文本向量化客户端。

    负责：
    - 单条/批量文本 → 向量
    - 内存 LRU 缓存（避免重复请求）
    - 自动批处理（API 单次限制内拆批）
    """

    DEFAULT_BATCH_SIZE = 10

    def __init__(self, client: Any | None = None) -> None:
        """初始化。

        Args:
            client: OpenAI 兼容客户端实例。None 时自动创建（延迟导入 openai）。
        """
        self._client = client
        self._cache: dict[str, list[float]] = {}  # md5(text) → vector
        self._batch_size = self.DEFAULT_BATCH_SIZE

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ImportError("openai 包未安装, 请执行: pip install openai") from exc

            cfg = settings.embedding
            self._client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

        return self._client

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def embed_text(self, text: str, *, use_cache: bool = True) -> list[float]:
        """单条文本 → 向量。"""
        if use_cache:
            key = self._cache_key(text)
            if key in self._cache:
                return self._cache[key]

        vectors, _ = self.embed_batch([text], use_cache=False)
        return vectors[0]

    def embed_batch(
        self,
        texts: list[str],
        *,
        use_cache: bool = True,
        model: str | None = None,
    ) -> tuple[list[list[float]], dict[str, Any]]:
        """批量文本 → 向量列表。

        Returns:
            (向量列表, 调用统计 {"total_tokens": int, "batch_count": int})
        """
        model = model or settings.embedding.model
        trace_id = get_trace_id()

        # 缓存命中过滤
        cached: dict[int, list[float]] = {}
        to_embed: list[tuple[int, str]] = []
        for i, text in enumerate(texts):
            key = self._cache_key(text)
            if use_cache and key in self._cache:
                cached[i] = self._cache[key]
            else:
                to_embed.append((i, text))

        if not to_embed:
            logger.debug("embed cache full hit", extra={"count": len(texts), "trace_id": trace_id})
            return [cached[i] for i in range(len(texts))], {"total_tokens": 0, "batch_count": 0}

        # 分批请求
        all_vectors: dict[int, list[float]] = dict(cached)
        total_tokens = 0
        batch_count = 0

        raw_texts = [t for _, t in to_embed]
        # 在 batch_size 内再次拆批
        for start in range(0, len(raw_texts), self._batch_size):
            batch = raw_texts[start:start + self._batch_size]
            batch_indices = [to_embed[start + j][0] for j in range(len(batch))]

            logger.debug("embed batch start", extra={"model": model, "batch_size": len(batch), "trace_id": trace_id})

            try:
                response = self.client.embeddings.create(model=model, input=batch)
            except Exception as exc:
                logger.error("embed batch failed", extra={"error": str(exc), "trace_id": trace_id})
                raise

            for j, item in enumerate(response.data):
                idx = batch_indices[j]
                vec = item.embedding
                all_vectors[idx] = vec
                # 写入缓存
                self._cache[self._cache_key(texts[idx])] = vec

            tokens = response.usage.total_tokens if response.usage else 0
            total_tokens += tokens
            batch_count += 1

        logger.info(
            "embed batch done",
            extra={"total": len(texts), "batches": batch_count, "tokens": total_tokens, "trace_id": trace_id},
        )

        result = [all_vectors[i] for i in range(len(texts))]
        return result, {"total_tokens": total_tokens, "batch_count": batch_count}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def clear_cache(self) -> None:
        self._cache.clear()
