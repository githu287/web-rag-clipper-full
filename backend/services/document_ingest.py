"""
DocumentIngestService：Document 生命周期编排层（Phase 2.9 Step 2；Phase 2.11 Step 2
扩展；Phase 3.4 Step C 增加 user_id 参数，实现 ownership check；
Phase 3.4 Step D 增加 api_key 参数，透传用户自己的百炼 API Key 到 Embedding）。

职责：
    将「Document 元数据生命周期」与「Milvus chunk 入库」串联为一条完整链路，
    以 Document 为事实来源驱动 ingest 流程：

      Step 1: document_repository.get_document(document_id, user_id)
              （Phase 3.4 Step C：ownership check —— 用户 A 无法 ingest 用户 B
               的文档，跨用户访问表现为 DocumentNotFoundError；在进入
               PROCESSING 之前完成）
              不存在 → 直接抛 DocumentNotFoundError，不做任何状态写入。
      Step 1.5 (Phase 2.11 Step 2): status == DELETING → 直接拒绝
              抛 DocumentOperationError，不进入 PROCESSING、不触碰 Milvus。
      Step 2: document_repository.update_status(
                  document_id, PROCESSING, error_message=None)
              进入 PROCESSING 时清空旧 error_message（retry 生命周期修复）；
              不修改 chunk_count。
      Step 3: ingest_service.ingest_page(page_id=document_id, chunks=chunks,
                                         user_id=user_id, api_key=api_key)
              （复用 Phase 2.6 现有 IngestService，纯 Milvus 编排：query old → embedding
                → upsert new → stale delete；api_key 为 Phase 3.4 Step D/F6 透传的
                当前用户自己的百炼 API Key，严禁回退服务器 Key）
      Step 4: 成功 → document_repository.update_ingest_result(
                         document_id, chunk_count=len(chunks),
                         status=SUCCESS, error_message=None)
              chunk_count + SUCCESS + 清空 error_message 在同一数据库事务中一次完成
              （不允许再额外调用 update_status(SUCCESS)）。
      Step 5: 失败（embedding / upsert / stale-delete / ingest_service 任意异常）
              → document_repository.update_failure(
                    document_id, error_message=截断摘要)
              原子落 status=FAILED + error_message=新错误；不修改 chunk_count
              （保持数据库原值）；原始异常继续向上传播，不吞异常；
              若 update_failure 自身也失败，不得覆盖原始业务异常。

映射关系（方案 A，已确认）：
    document.id == Milvus page_id（1:1）。本阶段不修改 Milvus Schema、
    不新增 document_id 字段，DocumentIngestService 仅通过 page_id=document_id
    复用现有 ingest_page 完成 Milvus 侧数据写入。

并发互斥（Phase 2.11 Step 2）：
    DELETING 状态下 ingest/retry 一律拒绝（Step 1.5 gate），保证
    DocumentDeleteService 删除流程中不会有新 chunks 写入 Milvus。

设计约束：
    - 依赖通过 Protocol 注入（DocumentRepository / IngestService），不直接 import
      pymilvus / openai / 百炼 SDK；
    - 不创建 Engine / Session（全部委托 DocumentRepository）；
    - 不处理 HTTP / FastAPI Request（API 层职责，本阶段不新增 Router）；
    - 异步说明：ingest_page 为 async def，本 Service 也以 async def 暴露入口；
      内部对同步 Repository 直接同步调用（不使用 asyncio.to_thread，不引入异步 SDK）。
"""

from __future__ import annotations

import logging

from ..core.exceptions import DocumentNotFoundError, DocumentOperationError
from ..models.document import DocumentStatus
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
        user_id: int,
        api_key: str | None = None,
    ) -> None:
        """
        以 Document 生命周期为骨架执行一次完整 ingest（方案 A：page_id = document_id）。

        流程见模块 docstring Step 1~5。

        Phase 3.4 Step C：新增 user_id 参数。Step 1 即用
        get_document(document_id, user_id) 完成 ownership check —— 用户 A 无法
        ingest 用户 B 的文档（跨用户访问表现为 DocumentNotFoundError），
        ownership check 在进入 PROCESSING 之前完成。DELETING gate 与
        PROCESSING/SUCCESS/FAILED 状态机不变。

        Phase 3.4 Step D：新增 api_key 参数 —— 当前用户的百炼 API Key
        （AuthService 解密后传入），透传至 Embedding；
        Phase 3.4 Step F6：api_key 不得为 None（Client 层已强制），
        严禁回退 settings.bailian_api_key。

        Args:
            document_id: Document 主键 ID（1:1 对应 Milvus page_id）。
            chunks: 已切分的 chunk 文本列表（Phase 2.10 前由调用方传入）。
            user_id: 当前用户 ID（归属校验，由认证上下文 / 测试显式传入）。
            api_key: 用户自己的百炼 API Key（Phase 3.4 Step D/F6；必填透传）。

        Raises:
            DocumentNotFoundError: document_id 不存在（Step 1 直接抛出，无状态写入）。
            DocumentRepositoryError: MySQL 更新失败（PROCESSING / SUCCESS / FAILED 任一步）。
            EmbeddingClientError / MilvusRepositoryError: Milvus 侧 ingest 失败
                （Step 5 已转 FAILED 后原始异常继续向上传播）。
            ValueError: chunks 含空字符串（由 EmbeddingClient.embed 内部校验抛出）。
        """
        # ---------------------------------------------------------- Step 1: 校验存在
        # Phase 3.4 Step C：get_document(document_id, user_id) 完成 ownership
        # check（跨用户访问 → DocumentNotFoundError），在进入 PROCESSING 之前。
        document = self._document_repo.get_document(document_id, user_id)
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

        # ---------------------------------------------------------- Step 2: 置 PROCESSING
        # 进入 PROCESSING 时清空旧 error_message（retry 生命周期修复）；不修改 chunk_count
        self._document_repo.update_status(
            document_id,
            DocumentStatus.PROCESSING,
            error_message=None,
        )
        logger.info(
            "DocumentIngestService.ingest_document: document_id=%s → PROCESSING",
            document_id,
        )

        # ---------------------------------------------------------- Step 3: Milvus ingest
        try:
            await self._ingest_service.ingest_page(
                page_id=document_id,
                chunks=chunks,
                user_id=user_id,
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
