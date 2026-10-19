"""Workspace-scoped retrieval. Blocking model and repository calls run in worker threads.
Results are filtered again by authoritative document ownership and SUCCESS state.
"""

from __future__ import annotations

import logging
import asyncio
from typing import Final

from ..clients.embedding import EmbeddingClient
from ..core.exceptions import DocumentNotFoundError, DocumentNotSuccessError
from ..models.api_schema import RagSearchResult
from ..models.document import DocumentStatus
from ..models.milvus_dto import ChunkSearchResult
from ..repositories.milvus import MilvusRepository
from ..repositories.mysql import DocumentRepository

logger: logging.Logger = logging.getLogger(__name__)

# Milvus 初始候选召回数下限（Phase 2.2 §14.3 / Phase 2.3 §7.1：固定扩大召回 top-10）。
# Phase 2.12 Step 2：实际召回数 = max(limit, 该下限)，保证 status post-filter 后
# 仍有机会凑满用户请求的 limit（limit ∈ [1, 20]）。
_MILVUS_SEARCH_CANDIDATE_LIMIT: int = 10

# 日志预览长度上限：success_ids 规模 = 该 Workspace 的 SUCCESS 文档数（SQL 层无 LIMIT），
# expr 又由它逐项拼接，直接 %r 会打出随文档数无界增长的日志行（可达数十 KB）。
# 收敛为定长预览，完整规模通过预览尾部标注的原始长度体现。
_LOG_PREVIEW_CHARS: Final[int] = 200


def _log_preview(value: object) -> str:
    """返回定长日志预览；超长时尾部标注未打印的原始长度。"""
    text = repr(value)
    if len(text) <= _LOG_PREVIEW_CHARS:
        return text
    return f"{text[:_LOG_PREVIEW_CHARS]}...(共 {len(text)} 字符)"


class RagService:
    """
    RAG 检索编排服务。

    构造依赖通过 DI 注入（core/di.py get_rag_service() 工厂）：
        embedding_client   : EmbeddingClient       — query 文本嵌入
        milvus_repository  : MilvusRepository       — Milvus 向量检索（Protocol 类型）
        document_repository: DocumentRepository     — MySQL documents 批量反查（Protocol 类型）
    """

    def __init__(
        self,
        embedding_client: EmbeddingClient,
        milvus_repository: MilvusRepository,
        document_repository: DocumentRepository,
    ) -> None:
        """
        注入 EmbeddingClient + MilvusRepository + DocumentRepository。

        Args:
            embedding_client: 百炼 Embedding 客户端实例（同步 embed 方法）。
            milvus_repository: Milvus Repository 实例（Protocol 类型，不依赖具体 Impl）。
            document_repository: Document Repository 实例（Protocol 类型，用于
                Milvus 检索后批量反查 Document.status，实现 SUCCESS-only 过滤）。
        """
        self._embedding: EmbeddingClient = embedding_client
        self._repo: MilvusRepository = milvus_repository
        self._documents: DocumentRepository = document_repository

    # ------------------------------------------------------------------ 对外入口
    async def search(
        self,
        query: str,
        limit: int = 5,
        document_id: int | None = None,
        plugin_id: str | None = None,
        api_key: str | None = None,
    ) -> list[RagSearchResult]:
        """Resolve workspace scope, embed, retrieve, then recheck ownership/state.

        Empty libraries and unavailable documents never call the embedding API.
        Document scope uses max(limit * 4, 40) candidates; full-library scope uses
        max(limit, 10). Final results retain Milvus order and authoritative metadata.
        """
        if document_id is not None:
            # 当前网页模式：document_id + plugin_id 一起进入 ownership check（Step C 已
            # 实现 get_document(document_id, plugin_id) SQL 层归属过滤）。
            document = await asyncio.to_thread(self._documents.get_document, document_id, plugin_id)
            if document is None:
                raise DocumentNotFoundError(
                    f"Document {document_id} 不存在或不属于当前插件工作空间"
                )
            if document.status != DocumentStatus.SUCCESS:
                raise DocumentNotSuccessError(
                    f"Document {document_id} 当前状态为 {document.status}，"
                    "非 SUCCESS 不可检索"
                )
            candidate_limit: int = max(limit * 4, 40)
            expr: str = f"page_id == {document_id}"
        else:
            # 全部知识库模式：禁止匿名访问——plugin_id 只能来自后端认证上下文，
            # 绝不允许客户端传入。None 时直接返回 []（不查库、不调用 Milvus）。
            if plugin_id is None:
                logger.warning(
                    "RagService.search: 全部知识库模式缺少 plugin_id，"
                    "拒绝匿名访问并返回空结果"
                )
                return []
            # success_ids 必须来自 Repository 的 get_success_document_ids（SQL 层
            # plugin_id + SUCCESS 双条件过滤，严禁先查全部文档再 Python 过滤）。
            success_ids: list[int] = await asyncio.to_thread(self._documents.get_success_document_ids, plugin_id)
            if not success_ids:
                logger.info(
                    "RagService.search: 插件工作空间 %s 无 SUCCESS document，"
                    "返回空结果（不调用 Milvus）",
                    plugin_id,
                )
                return []
            candidate_limit = max(limit, _MILVUS_SEARCH_CANDIDATE_LIMIT)
            expr = f"page_id in [{','.join(map(str, success_ids))}]"

        # Resolve access and searchable scope before spending an embedding call.
        vectors = await asyncio.to_thread(self._embedding.embed, [query], api_key=api_key)
        query_vector = vectors[0]

        candidates: list[ChunkSearchResult] = await asyncio.to_thread(
            self._repo.search, query_vector,
            limit=candidate_limit,
            expr=expr,
        )
        logger.info(
            "RagService.search: document_id=%r, Milvus 候选召回数=%d, "
            "candidate_limit=%d, expr=%s, 最终 limit=%d",
            document_id,
            len(candidates),
            candidate_limit,
            _log_preview(expr),
            limit,
        )

        # ---------------------------------------------------------- Step 3: SUCCESS-only post-filter
        if not candidates:
            return []

        # 3.1 收集 page_id 并去重（dict.fromkeys 保持首次出现顺序，便于测试断言）。
        page_ids: list[int] = list(dict.fromkeys(c.page_id for c in candidates))

        # 3.2 一次批量反查 Document（空 page_ids 已被上方 candidates 空判断短路）。
        # Phase 3.5 Step 2-E：SQL 层 plugin_id 归属过滤（契约已 plugin_id 化）。
        documents = await asyncio.to_thread(self._documents.get_documents_by_ids, page_ids, plugin_id)

        # 3.3 只保留 SUCCESS；FAILED / DELETING / PENDING / PROCESSING 全部过滤；
        #     不存在的 Document（孤儿 chunk）因不在 success 集合中也被过滤。
        #     用 {doc.id: doc} 建立 ID 映射（Phase 2.13 Step 2）：
        #     - get_documents_by_ids 的 SQL IN 查询不保证返回顺序，禁止用列表位置关联；
        #     - 后续组装 metadata 时按 document_map[c.page_id] 取值。
        document_map = {document.id: document for document in documents}
        success_document_ids = {
            document.id
            for document in documents
            if document.status == DocumentStatus.SUCCESS
        }
        filtered = [
            c for c in candidates if c.page_id in success_document_ids
        ]
        logger.info(
            "RagService.search: page_ids=%r, SUCCESS 文档=%r, "
            "过滤后候选=%d",
            page_ids,
            sorted(success_document_ids),
            len(filtered),
        )

        # ---------------------------------------------------------- Step 3.6: Workspace 隔离最终兜底（Phase 3.5 Step 2-E）
        # 应用层双保险（Milvus expr 之外的最终防线）：
        #   - 当前网页模式：仅保留 candidate.page_id == document_id；
        #   - 全部知识库模式：仅保留 candidate.page_id in success_ids
        #     （即使 Milvus / MySQL 出现异常返回，也绝不允许跨 Workspace 候选泄漏）。
        if document_id is not None:
            filtered = [c for c in filtered if c.page_id == document_id]
            logger.info(
                "RagService.search: document_id=%r 过滤后候选=%d",
                document_id,
                len(filtered),
            )
        else:
            filtered = [c for c in filtered if c.page_id in success_ids]
            logger.info(
                "RagService.search: 插件工作空间 %s success_ids(%d 个)=%s 最终兜底后候选=%d",
                plugin_id,
                len(success_ids),
                _log_preview(success_ids),
                len(filtered),
            )

        # ---------------------------------------------------------- Step 4: final top-K + metadata
        results: list[RagSearchResult] = []
        for candidate in filtered[:limit]:
            document = document_map[candidate.page_id]  # SUCCESS 集合保证存在，不会 KeyError
            results.append(
                RagSearchResult(
                    id=candidate.id,
                    page_id=candidate.page_id,
                    chunk_index=candidate.chunk_index,
                    chunk_text=candidate.chunk_text,
                    distance=candidate.distance,
                    document_id=document.id,  # document.id = Milvus.page_id（1:1）
                    filename=document.filename,
                    status=document.status,
                    created_at=document.created_at,
                    # Phase 3.1 Step 3：网页剪藏来源元数据（来源 documents 表，
                    # 经既有 document_map 关联填充，不新增 SQL / N+1）
                    title=document.title,
                    url=document.url,
                    source_type=document.source_type,
                )
            )
        return results
