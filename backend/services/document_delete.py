"""Delete Milvus chunks, local files, then the owned MySQL document.

An atomic state transition excludes concurrent ingestion. Partial failures keep
DELETING so retrieval cannot advertise partially removed data as successful.
Repeated deletion resumes cleanup; missing documents are idempotent success.
"""

from __future__ import annotations

import asyncio
import logging

from ..core.exceptions import (
    DocumentNotFoundError,
    DocumentOperationError,
)
from ..models.document import DocumentSourceType, DocumentStatus
from ..repositories.milvus import MilvusRepository
from ..repositories.mysql import DocumentRepository
from ..storage import FileStorage

logger: logging.Logger = logging.getLogger(__name__)


class DocumentDeleteService:
    """
    Document 删除编排服务。

    构造依赖通过 DI 注入（core/di.get_document_delete_service 工厂）：
        document_repository: DocumentRepository — documents 表数据访问（Protocol 类型）
        milvus_repository   : MilvusRepository   — Milvus chunks 删除（Protocol 类型）
        file_storage        : FileStorage        — 原始文件物理删除（Protocol 类型）
    """

    def __init__(
        self,
        document_repository: DocumentRepository,
        milvus_repository: MilvusRepository,
        file_storage: FileStorage,
    ) -> None:
        """
        Args:
            document_repository: DocumentRepository（Protocol），MySQL documents 表访问。
            milvus_repository: MilvusRepository（Protocol），chunk 查询 / 删除。
            file_storage: FileStorage（Protocol），原始文件物理删除（幂等）。
        """
        self._document_repo: DocumentRepository = document_repository
        self._milvus_repo: MilvusRepository = milvus_repository
        self._file_storage: FileStorage = file_storage

    # ------------------------------------------------------------------ 对外入口
    async def delete_document(self, document_id: int, plugin_id: str) -> None:
        """
        执行一次完整的 Document 删除（Milvus → FileStorage → MySQL，幂等）。

        不存在 / 已删除（DocumentNotFoundError）视为目标态已达成，静默成功。

        Phase 3.5 Step 2-E：plugin_id 用于 Step 1 ownership check（get_document
        (document_id, plugin_id)）与 Step 6 的 delete_document(document_id, plugin_id)。
        插件 A 删除插件 B 的文档按「不存在」处理（不泄露归属）。
        Milvus → FileStorage → MySQL 的删除顺序不变。

        Args:
            document_id: Document 主键 ID（1:1 对应 Milvus page_id）。
            plugin_id: 当前插件工作空间 ID（归属校验，由认证上下文 / 测试显式传入）。

        Raises:
            DocumentOperationError: status == PROCESSING（ingest 进行中拒绝删除）
                或 MySQL 更新失败。
            MilvusRepositoryError: Milvus 侧删除失败（保持 DELETING 供重试）。
            DocumentStorageError: FileStorage 删除失败（保持 DELETING 供重试）。
        """
        # All dependencies use synchronous I/O. Keep the ordered cleanup on one
        # worker thread so slow storage cannot stall unrelated API requests.
        await asyncio.to_thread(self._delete_document_sync, document_id, plugin_id)

    def _delete_document_sync(self, document_id: int, plugin_id: str) -> None:
        # ---------------------------------------------------------- Step 1: 存在性校验
        # Phase 3.5 Step 2-E：workspace-aware ownership check（跨 Workspace → 不存在）
        try:
            document = self._document_repo.get_document(document_id, plugin_id)
        except DocumentNotFoundError:
            # 幂等：目标态「文档不存在」已达成
            logger.info(
                "DocumentDeleteService.delete_document: document_id=%s 不存在，"
                "视为删除已完成（幂等成功）",
                document_id,
            )
            return

        # ---------------------------------------------------------- Step 2: 并发 gate
        if document.status == DocumentStatus.PROCESSING:
            raise DocumentOperationError(
                f"document is being ingested, delete rejected: id={document_id}"
            )
        # DELETING → 允许继续删除（重复 DELETE / 上次中断续删）；其余状态进入删除
        original_status: str = document.status
        logger.info(
            "DocumentDeleteService.delete_document: document_id=%s, "
            "original_status=%s, file_path=%s",
            document_id,
            original_status,
            document.file_path,
        )

        # ---------------------------------------------------------- Step 3: 置 DELETING
        self._document_repo.transition_status(
            document_id, plugin_id,
            {DocumentStatus.PENDING, DocumentStatus.SUCCESS, DocumentStatus.FAILED, DocumentStatus.DELETING},
            DocumentStatus.DELETING,
        )
        logger.info(
            "DocumentDeleteService.delete_document: document_id=%s → DELETING",
            document_id,
        )

        # ---------------------------------------------------------- Step 4: Milvus 删除
        # 每次删除都重新 query 当前实际存在的 chunk IDs，不假设上一次删除未生效
        try:
            chunk_ids = self._milvus_repo.query_page_chunks(document_id)
            logger.info(
                "DocumentDeleteService.delete_document: document_id=%s "
                "Milvus 待删 chunks=%d",
                document_id,
                len(chunk_ids),
            )
            if chunk_ids:
                self._milvus_repo.delete_chunks(chunk_ids)
        except Exception:
            # Keep DELETING until a retry finishes resource cleanup.
            raise

        # ---------------------------------------------------------- Step 5: FileStorage 删除
        # Phase 3.1 Step 3：仅 source_type == "upload" 才删除物理文件；
        # webpage 剪藏不落盘（file_path=""），跳过 FileStorage，但仍删除
        # Milvus chunks 与 MySQL document 行（DELETE 保持 204 幂等）。
        try:
            if document.source_type == DocumentSourceType.UPLOAD:
                self._file_storage.delete(document.file_path)
        except Exception:
            # MySQL document 行必须保留，不允许直接删除 MySQL document
            # Keep DELETING until a retry finishes resource cleanup.
            raise

        # ------------------------------------------------------ Step 6: MySQL 删除（提交点）
        try:
            # Phase 3.5 Step 2-E：workspace-aware（WHERE id + plugin_id）
            self._document_repo.delete_document(document_id, plugin_id)
        except DocumentNotFoundError:
            # 并发请求已删除 → 幂等成功
            logger.info(
                "DocumentDeleteService.delete_document: document_id=%s 已被并发"
                "请求删除，幂等成功",
                document_id,
            )
            return
        except Exception:
            # 保持 DELETING（不回滚），下一次 DELETE 重新 query 后收敛
            logger.exception(
                "DocumentDeleteService.delete_document: document_id=%s "
                "MySQL 删除失败，保持 DELETING 供下次 DELETE 收敛",
                document_id,
            )
            raise

        logger.info(
            "DocumentDeleteService.delete_document: document_id=%s 删除完成",
            document_id,
        )
