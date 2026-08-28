"""
DocumentDeleteService：Document 删除编排层（Phase 2.11 Step 2；Phase 3.5 Step 2-E
增加 plugin_id 参数，实现 workspace-aware 删除）。

职责：
    将「Milvus chunks 删除 → FileStorage 物理文件删除 → MySQL document 行删除」
    串联为一条最终一致的删除链路，MySQL document 行删除为最终提交点。

流程（严格顺序）：
      Step 1: get_document(document_id, plugin_id)
              （Phase 3.5 Step 2-E：workspace-aware —— 插件 A 删除插件 B 的文档按
               「不存在」处理，不泄露归属）
              不存在（DocumentNotFoundError）→ 幂等成功，直接返回
              （目标态「文档不存在」已达成，Router 返回 204）。
      Step 2: status == PROCESSING → 拒绝删除，抛 DocumentOperationError
              （ingest 进行中，禁止并发删除）。
              status == DELETING → 允许继续删除（重复 DELETE / 上次中断续删）。
              PENDING / SUCCESS / FAILED → 进入删除。
      Step 3: 记录 original_status；update_status(document_id, DELETING)
              （DELETING 为 Delete ↔ Ingest/Retry 并发互斥标记）。
      Step 4: Milvus：query_page_chunks(page_id=document_id) 获取当前实际存在的
              chunk IDs → delete_chunks(ids)。
              必须「每次删除都重新 query」，不得假设上一次删除完全没有生效
              （Milvus delete 可能部分成功后失败）。
              失败 → 尝试恢复 original_status → 原异常继续传播（HTTP 503）。
      Step 5: FileStorage：file_storage.delete(document.file_path)（幂等，
              文件已不存在视为成功）。
              失败 → 尝试恢复 original_status → 原异常继续传播（HTTP 5xx）；
              MySQL document 行必须保留。
      Step 6: MySQL：delete_document(document_id, plugin_id)（最终提交点，
              Phase 3.5 Step 2-E 同样 workspace-aware：跨 Workspace 按「不存在」处理）。
              DocumentNotFoundError → 并发请求已删除，幂等成功，返回。
              其他异常 → 保持 DELETING（不回滚），原异常继续传播（HTTP 503）；
              下一次 DELETE 走 Step 4 重新 query → 收敛。

失败状态规则（与状态机一致）：
    - Milvus / FileStorage 删除失败：DELETING → 原状态（仅 MySQL 状态恢复，
      不代表 Milvus 物理数据已回滚）。
    - MySQL 删除失败：保持 DELETING（禁止恢复为 SUCCESS / FAILED / PENDING）。
    - 恢复状态失败仅记录日志，绝不覆盖原始删除异常。

并发与幂等：
    - DELETE 幂等：重复调用 / 并发调用均可收敛到「文档不存在」终态。
    - 不引入版本号 / CAS / MQ / Outbox / Saga。

映射关系（方案 A，已确认）：
    document.id == Milvus page_id（1:1）。不修改 Milvus Schema、
    不新增 document_id 字段，通过 page_id=document_id 复用现有
    query_page_chunks / delete_chunks。
"""

from __future__ import annotations

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
            MilvusRepositoryError: Milvus 侧删除失败（状态已尝试恢复后继续传播）。
            DocumentStorageError: FileStorage 删除失败（状态已尝试恢复后继续传播）。
        """
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
        self._document_repo.update_status(
            document_id,
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
            self._milvus_repo.delete_chunks(chunk_ids)
        except Exception:
            # 尝试恢复原状态（仅 MySQL 状态，不代表 Milvus 物理数据已回滚）
            self._restore_status(document_id, original_status)
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
            self._restore_status(document_id, original_status)
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

    # ------------------------------------------------------------ 内部辅助
    def _restore_status(self, document_id: int, status: str) -> None:
        """
        删除失败时尝试恢复 Document 状态（仅 MySQL 状态）。

        注意：状态恢复不代表 Milvus 物理数据已回滚；恢复失败仅记录日志，
        绝不覆盖原始删除异常。
        """
        try:
            self._document_repo.update_status(document_id, status)
        except Exception:  # noqa: BLE001
            logger.exception(
                "DocumentDeleteService._restore_status: document_id=%s "
                "恢复 status=%s 失败（原始删除异常继续传播）",
                document_id,
                status,
            )
