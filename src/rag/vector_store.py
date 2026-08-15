# ChromaDB 向量库封装

"""向量存储 — ChromaDB 封装（增强版）。

增强特性（v2）:
  - 分页获取文档（避免 get_all_documents 全量 OOM）
  - 备份/恢复
  - 元数据索引提示（提升过滤查询性能）
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, cast

from core.logger import get_trace_id, logger
from core.settings import settings


class VectorStore:
    """ChromaDB 向量数据库封装。

    用法：
        store = VectorStore("my_collection")
        store.add(texts, vectors, metadatas, ids)
        results = store.search(query_vector, top_k=5)
    """

    def __init__(
        self,
        collection_name: str = "openclaw_kb",
        persist_path: str | None = None,
    ) -> None:
        try:
            import chromadb
        except ImportError:
            raise ImportError("chromadb 未安装, 请执行: pip install chromadb") from None

        persist_path = persist_path or settings.rag.vector_store_path
        self._persist_path = persist_path
        self._client = chromadb.PersistentClient(path=persist_path)
        self._name = collection_name
        self._collection = self._client.get_or_create_collection(name=collection_name)

    def _ensure_collection(self) -> None:
        """若 collection 引用失效（被删除或损坏），自动重新获取/创建。"""
        try:
            self._collection.count()
        except Exception as exc:
            err_msg = str(exc).lower()
            if "does not exist" in err_msg or "invalid collection" in err_msg or "not found" in err_msg:
                self._collection = self._client.get_or_create_collection(name=self._name)
            else:
                raise

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def add(
        self,
        texts: list[str],
        vectors: list[list[float]],
        metadatas: list[dict[str, Any]],
        ids: list[str] | None = None,
    ) -> list[str]:
        """批量写入向量 + 原文 + 元数据。

        Returns:
            写入的 document ids 列表
        """
        if ids is None:
            ids = [str(uuid.uuid4()) for _ in texts]

        trace_id = get_trace_id()
        logger.debug("vector_store add start", extra={"count": len(texts), "trace_id": trace_id})

        self._ensure_collection()
        self._collection.add(
            ids=ids,
            # chromadb 类型别名基于不可变 Sequence/Mapping（invariance），cast 消除 mypy 不变性报错
            embeddings=cast(Any, vectors),
            documents=texts,
            metadatas=cast(Any, metadatas),
        )

        logger.info(
            "vector_store add done", extra={"collection": self._name, "count": len(texts), "trace_id": trace_id}
        )
        return ids

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(
        self,
        query_vector: list[float],
        top_k: int | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """向量相似度检索（余弦相似度）。

        Returns:
            [{"id": str, "text": str, "metadata": dict, "score": float}, ...]
        """
        top_k = top_k or settings.rag.top_k_retrieve
        trace_id = get_trace_id()

        logger.debug("vector_search start", extra={"top_k": top_k, "where": where, "trace_id": trace_id})

        self._ensure_collection()
        results = self._collection.query(
            query_embeddings=cast(Any, [query_vector]),
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        items: list[dict[str, Any]] = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][i] if results["distances"] else 0.0
                score = 1.0 / (1.0 + distance)

                items.append(
                    {
                        "id": doc_id,
                        "text": results["documents"][0][i] if results["documents"] else "",
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                        "score": round(score, 4),
                    }
                )

        logger.info("vector_search done", extra={"found": len(items), "trace_id": trace_id})
        return items

    # ------------------------------------------------------------------
    # 分页获取（替代 get_all_documents 全量加载）
    # ------------------------------------------------------------------

    def get_documents_paginated(
        self,
        offset: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """分页获取文档列表。

        Args:
            offset: 起始偏移
            limit: 每页数量

        Returns:
            [{"id": str, "text": str, "metadata": dict}, ...]
        """
        self._ensure_collection()
        total = self.count()
        if offset >= total:
            return []

        # ChromaDB get 不支持 offset/limit，一次性加载后切片
        # 对于超大集合用 include offset/limit 的变通方案
        results = self._collection.get(
            limit=limit,
            offset=offset,
            include=["documents", "metadatas"],
        )

        items: list[dict[str, Any]] = []
        if results["ids"]:
            for i, doc_id in enumerate(results["ids"]):
                items.append(
                    {
                        "id": doc_id,
                        "text": results["documents"][i] if results["documents"] else "",
                        "metadata": results["metadatas"][i] if results["metadatas"] else {},
                    }
                )
        return items

    def get_all_documents(self) -> list[dict[str, Any]]:
        """获取全部文档（仅适用于小型集合，大数据集用 get_documents_paginated）。

        Returns:
            [{"id": str, "text": str, "metadata": dict}, ...]
        """
        self._ensure_collection()
        total = self.count()
        if total > 5000:
            logger.warning(
                f"get_all_documents: 集合 {self._name} 有 {total} 条记录，"
                f"全量加载可能导致 OOM。建议使用 get_documents_paginated()"
            )

        results = self._collection.get(include=["documents", "metadatas"])
        items: list[dict[str, Any]] = []
        if results["ids"]:
            for i, doc_id in enumerate(results["ids"]):
                items.append(
                    {
                        "id": doc_id,
                        "text": results["documents"][i] if results["documents"] else "",
                        "metadata": results["metadatas"][i] if results["metadatas"] else {},
                    }
                )
        return items

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    def delete(self, ids: list[str]) -> None:
        """按 ID 批量删除。"""
        if ids:
            self._ensure_collection()
            self._collection.delete(ids=ids)
            logger.info("vector_store delete", extra={"collection": self._name, "count": len(ids)})

    def delete_by_metadata(self, where: dict[str, Any]) -> int:
        """按元数据条件批量删除。

        Returns:
            删除的文档数
        """
        self._ensure_collection()
        before = self.count()
        # ChromaDB where 条件使用 $eq 操作符确保兼容性
        op_where = {k: {"$eq": v} for k, v in where.items()}
        self._collection.delete(where=cast(Any, op_where))
        after = self.count()
        deleted = before - after
        logger.info(
            "vector_store delete by metadata", extra={"collection": self._name, "deleted": deleted, "where": op_where}
        )
        return deleted

    def delete_collection(self) -> None:
        """清空 collection 中的所有文档（保留 collection 本身，避免引用失效）。"""
        self._ensure_collection()
        # 分页获取全部 ID 后删除（get() 默认 limit=10，必须分页）
        all_ids: list[str] = []
        offset = 0
        batch_size = 500
        while True:
            results = self._collection.get(offset=offset, limit=batch_size, include=[])
            if not results or not results.get("ids"):
                break
            ids = results["ids"]
            if not ids:
                break
            all_ids.extend(ids)
            if len(ids) < batch_size:
                break
            offset += batch_size

        if all_ids:
            self._collection.delete(ids=all_ids)
        # 防御性重建：重新获取 collection 引用，防止某些 ChromaDB 版本在清空后引用失效
        self._collection = self._client.get_or_create_collection(name=self._name)
        logger.info("vector_store collection cleared", extra={"collection": self._name, "deleted": len(all_ids)})

    def count(self) -> int:
        """文档总数。"""
        self._ensure_collection()
        return self._collection.count()

    # ------------------------------------------------------------------
    # 备份 / 恢复
    # ------------------------------------------------------------------

    def backup(self, backup_dir: str | Path) -> Path:
        """备份整个向量库到指定目录。

        Args:
            backup_dir: 备份目标目录

        Returns:
            备份目录路径
        """
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)

        src = Path(self._persist_path)
        if not src.exists():
            raise FileNotFoundError(f"向量库路径不存在: {src}")

        # 复制整个 ChromaDB 持久化目录
        dst = backup_dir / f"{self._name}_{uuid.uuid4().hex[:8]}"
        shutil.copytree(src, dst, dirs_exist_ok=True)

        logger.info("vector_store backup done", extra={"from": str(src), "to": str(dst)})
        return dst

    @staticmethod
    def restore(
        backup_path: str | Path,
        collection_name: str,
        target_persist_path: str | None = None,
    ) -> VectorStore:
        """从备份恢复到新的 VectorStore 实例。

        Args:
            backup_path: 备份目录路径
            collection_name: 目标 collection 名称
            target_persist_path: 目标持久化路径（默认使用 settings）

        Returns:
            新的 VectorStore 实例
        """
        backup_path = Path(backup_path)
        target_path = Path(target_persist_path or settings.rag.vector_store_path)

        if not backup_path.exists():
            raise FileNotFoundError(f"备份路径不存在: {backup_path}")

        # 复制到目标路径
        if backup_path != target_path:
            shutil.copytree(backup_path, target_path, dirs_exist_ok=True)

        return VectorStore(collection_name=collection_name, persist_path=str(target_path))
