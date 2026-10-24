"""Document lifecycle boundary for chunk ingestion.

Validate workspace ownership and acquire PROCESSING atomically before writing
vectors. Parser/clip services may pass their internal preclaimed flag after
claiming the document themselves. Success commits chunk count and status;
failures preserve the document for retry.
"""

from __future__ import annotations

import logging

from ..core.exceptions import DocumentNotFoundError, DocumentOperationError
from ..models.document import DocumentStatus
from ..models.model_provider import embedding_claim_options
from ..repositories.mysql import DocumentRepository
from .ingest import IngestService

logger: logging.Logger = logging.getLogger(__name__)

# 错误摘要最大长度（与 DocumentUploadService 对齐：error_message 最多保存 2048 字符）
_MAX_ERROR_MESSAGE_LENGTH: int = 2048


def _build_error_summary(exc: Exception) -> str:
    """构造写入 error_message 的截断摘要（与 DocumentUploadService 逻辑一致）。"""
    summary = str(exc)[:_MAX_ERROR_MESSAGE_LENGTH]
    if not summary:
        summary = exc.__class__.__name__
    return summary


class DocumentIngestService:
    """
    Document 生命周期编排服务。

    构造依赖通过 DI 注入（core/di.get_document_ingest_service 工厂）：
        document_repository: DocumentRepository  — documents 表数据访问（Protocol 类型）
        ingest_service     : IngestService        — Milvus chunk 入库编排（现有服务）
    """

    def __init__(
        self,
        document_repository: DocumentRepository,
        ingest_service: IngestService,
    ) -> None:
        """
        Args:
            document_repository: DocumentRepository（Protocol），MySQL documents 表访问。
            ingest_service: 现有 IngestService，纯 Milvus 编排（本类不修改它）。
        """
        self._document_repo: DocumentRepository = document_repository
        self._ingest_service: IngestService = ingest_service

    # ------------------------------------------------------------------ 对外入口
    async def ingest_document(
        self,
        document_id: int,
        chunks: list[str],
        plugin_id: str,
        api_key: str | None = None,
        *,
        preclaimed: bool = False,
    ) -> None:
        """
        以 Document 生命周期为骨架执行一次完整 ingest（方案 A：page_id = document_id）。

        流程见模块 docstring Step 1~5。

        Phase 3.5 Step 2-E：新增 plugin_id 参数。Step 1 即用
        get_document(document_id, plugin_id) 完成 ownership check —— 插件 A 无法
        ingest 插件 B 的文档（跨 Workspace 访问表现为 DocumentNotFoundError），
        ownership check 在进入 PROCESSING 之前完成。DELETING gate 与
        PROCESSING/SUCCESS/FAILED 状态机不变。

        Phase 3.4 Step D：新增 api_key 参数 —— 当前插件工作空间的百炼 API Key
        （PluginService 解密后传入），透传至 Embedding；
        Phase 3.4 Step F6：api_key 不得为 None（Client 层已强制），
        严禁回退 settings.bailian_api_key。

        Args:
            document_id: Document 主键 ID（1:1 对应 Milvus page_id）。
            chunks: 已切分的 chunk 文本列表（Phase 2.10 前由调用方传入）。
            plugin_id: 当前插件工作空间 ID（归属校验，由认证上下文 / 测试显式传入）。
            api_key: 插件工作空间自己的百炼 API Key（Phase 3.4 Step D/F6；必填透传）。

        Raises:
            DocumentNotFoundError: document_id 不存在（Step 1 直接抛出，无状态写入）。
            DocumentRepositoryError: MySQL 更新失败（PROCESSING / SUCCESS / FAILED 任一步）。
            EmbeddingClientError / MilvusRepositoryError: Milvus 侧 ingest 失败
                （Step 5 已转 FAILED 后原始异常继续向上传播）。
            ValueError: chunks 含空字符串（由 EmbeddingClient.embed 内部校验抛出）。
        """
        # ---------------------------------------------------------- Step 1: 校验存在
        # Phase 3.5 Step 2-E：get_document(document_id, plugin_id) 完成 ownership
        # check（跨 Workspace 访问 → DocumentNotFoundError），在进入 PROCESSING 之前。
        document = self._document_repo.get_document(document_id, plugin_id)
        # 返回对象暂不读取字段；此处仅做存在性校验（不存在已抛 DocumentNotFoundError）
        logger.info(
            "DocumentIngestService.ingest_document: document_id=%s, "
            "当前 status=%s, chunk_count=%s",
            document_id,
            document.status,
            document.chunk_count,
        )

        # -------------------------------------------- Step 1.5: DELETING 并发 gate
        # 删除进行中禁止 ingest/retry 进入（不写 PROCESSING、不触碰 Milvus）
        if document.status == DocumentStatus.DELETING:
            raise DocumentOperationError(
                f"document is being deleted, ingest rejected: id={document_id}"
            )
        if not chunks:
            raise ValueError("chunks must not be empty")

        # ---------------------------------------------------------- Step 2: 置 PROCESSING
        # 进入 PROCESSING 时清空旧 error_message（retry 生命周期修复）；不修改 chunk_count
        if not preclaimed:
            self._document_repo.transition_status(
                document_id, plugin_id,
                {DocumentStatus.PENDING, DocumentStatus.SUCCESS, DocumentStatus.FAILED},
                DocumentStatus.PROCESSING,
                **embedding_claim_options(api_key),
            )
        elif document.status != DocumentStatus.PROCESSING:
            raise DocumentOperationError("document processing claim was lost")
        logger.info(
            "DocumentIngestService.ingest_document: document_id=%s → PROCESSING",
            document_id,
        )

        # ---------------------------------------------------------- Step 3: Milvus ingest
        try:
            await self._ingest_service.ingest_page(
                page_id=document_id,
                chunks=chunks,
                plugin_id=plugin_id,
                api_key=api_key,
            )
        except Exception as exc:
            # ------------------------------------------------------ Step 5: 失败路径
            # 原子落 status=FAILED + error_message=新错误；不修改 chunk_count
            # （保持数据库原值）；原始异常继续向上传播（不吞异常）
            logger.exception(
                "DocumentIngestService.ingest_document: document_id=%s 失败，"
                "标记 FAILED（chunk_count 保持原值）",
                document_id,
            )
            try:
                self._document_repo.update_failure(
                    document_id,
                    error_message=_build_error_summary(exc),
                )
            except Exception:
                # update_failure 自身失败：记录日志，绝不覆盖原始业务异常
                logger.exception(
                    "DocumentIngestService.ingest_document: document_id=%s "
                    "更新 FAILED 状态失败，保留原始 ingest 异常",
                    document_id,
                )
            # 原始异常继续向上传播（不吞异常）
            raise

        # ---------------------------------------------------------- Step 4: 成功路径
        # chunk_count + SUCCESS + 清空 error_message 单事务一次完成；
        # 不允许额外 update_status(SUCCESS)
        self._document_repo.update_ingest_result(
            document_id,
            chunk_count=len(chunks),
            status=DocumentStatus.SUCCESS,
            error_message=None,
        )
        logger.info(
            "DocumentIngestService.ingest_document: document_id=%s → SUCCESS, "
            "chunk_count=%d",
            document_id,
            len(chunks),
        )
