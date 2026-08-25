"""
WebClipService（Phase 3.1 Step 3）。

让后端具备「网页剪藏接收能力」：未来 Browser Extension 只需把网页 URL /
标题 / 正文纯文本发给后端（POST /clips），本服务完成全链路入库：

    WebClipService.clip()
        ↓
    create_document(PENDING)                    # filename=webclip.txt, file_path=""
        ↓
    update_status(PROCESSING)                   # 必须在 Chunker 之前（状态机红线）
        ↓
    Chunker.split(raw_text) → list[str]
        ↓
    DocumentIngestService.ingest_document()     # Embedding → Milvus Upsert + Stale Delete
        ↓
    SUCCESS / FAILED

设计要点（与 DocumentUploadService 对齐，但去掉 Parser / FileStorage）：
1) WebClip 不创建物理文件：file_path 固定 ""，filename 固定 "webclip.txt"
   （禁止用 title / URL / webclip_<id>.txt 作 filename，避免新增二次更新
   filename 的 Repository 方法）；
2) source_type 固定 "webpage"，不允许客户端传入；
3) 状态机红线（与 Phase 2.10 已修复问题一致）：
   - PROCESSING 必须发生在 Chunker 之前；
   - Chunker 失败：PROCESSING → FAILED（update_failure 写截断 error_message，
     chunk_count 保持旧值），**原异常继续向上传播**；
   - 不存在 PENDING → FAILED / PENDING → SUCCESS 非法迁移；
4) DocumentIngestService 已具备 DELETING gate / PROCESSING → SUCCESS /
   PROCESSING → FAILED / error_message 生命周期 / chunk_count 更新 / Milvus
   ingest，本服务**不得重复实现**这些逻辑：ingest_document 内部的失败
   处理与异常传播完全委托给它；
5) update_status(PROCESSING) 自身失败直接向上传播（Document 保持 PENDING，
   不误标 FAILED），与 DocumentUploadService 行为一致；
6) 本服务不建 Protocol：直接普通 Service，复用 DocumentRepository / Chunker /
   DocumentIngestService。

禁止修改：DocumentIngestService / IngestService / DocumentUploadService /
FileStorage / MilvusRepository / Milvus Schema / ChunkVector / EmbeddingClient。
"""

from __future__ import annotations

import logging

from ..chunkers import Chunker
from ..models.document import Document, DocumentSourceType, DocumentStatus
from ..repositories.mysql import DocumentRepository
from .document_ingest import DocumentIngestService

logger = logging.getLogger(__name__)

# WebClip 固定文件名（不落盘，仅为 documents.filename 列占位）
_WEB_CLIP_FILENAME = "webclip.txt"

# WebClip 固定逻辑路径（无物理文件）
_WEB_CLIP_FILE_PATH = ""

# error_message 最大长度（与 DocumentUploadService._MAX_ERROR_MESSAGE_LENGTH 对齐）
_MAX_ERROR_MESSAGE_LENGTH = 2048


class WebClipService:
    """
    网页剪藏编排服务：建 Document → PROCESSING → Chunker → ingest。

    Args:
        document_repository: DocumentRepository（Protocol）——创建 / 状态 / 失败落盘。
        chunker: Chunker（Protocol）——str → list[str]。
        document_ingest_service: DocumentIngestService——Embedding + Milvus 入库
            （含 PROCESSING → SUCCESS/FAILED 全生命周期，本服务不重复实现）。
    """

    def __init__(
        self,
        document_repository: DocumentRepository,
        chunker: Chunker,
        document_ingest_service: DocumentIngestService,
    ) -> None:
        self._document_repository = document_repository
        self._chunker = chunker
        self._document_ingest_service = document_ingest_service

    async def clip(
        self,
        url: str,
        raw_text: str,
        user_id: int,
        title: str | None = None,
        api_key: str | None = None,
    ) -> Document:
        """
        执行完整网页剪藏链路，返回最终 Document（成功为 SUCCESS，失败抛原异常）。

        Phase 3.4 Step C：新增必填 user_id 参数 —— create_document 以
        user_id=user_id 落库；本服务不生成 / 不推断 user_id，由认证上下文 /
        测试显式传入。

        Phase 3.4 Step D：新增 api_key 参数 —— 当前用户的百炼 API Key
        （AuthService 解密后传入），透传至 Embedding；None 时回退
        settings.bailian_api_key（仅本地测试 / 兼容旧调用）。

        Args:
            url: 网页来源 URL（非空，由 API 层 WebClipRequest 校验）。
            raw_text: 网页正文纯文本（非空，由 API 层 WebClipRequest 校验）。
            user_id: 当前用户 ID（归属字段，必填）。
            title: 网页标题（可选；None 时写入 NULL）。
            api_key: 用户自己的百炼 API Key（Phase 3.4 Step D；None 回退测试 Key）。

        Returns:
            Document: 终态对象（SUCCESS，chunk_count = 实际入库数，
            title / url / source_type="webpage" 正确落库）。

        Raises:
            DocumentChunkingError: Chunker 切分失败（Document 已置 FAILED +
                error_message，原异常继续传播）。
            DocumentRepositoryError / MilvusRepositoryError / EmbeddingClientError:
                ingest 链路失败（Document 状态迁移由 DocumentIngestService 完成，
                原异常继续传播）。
        """
        # ------------------------------------------------------------------
        # 1) 创建 Document(status=PENDING)；WebClip 不落盘
        # ------------------------------------------------------------------
        document = self._document_repository.create_document(
            filename=_WEB_CLIP_FILENAME,
            file_path=_WEB_CLIP_FILE_PATH,
            user_id=user_id,
            title=title,
            url=url,
            source_type=DocumentSourceType.WEBPAGE,
        )

        # ------------------------------------------------------------------
        # 2) 立即置 PROCESSING（状态机红线：必须发生在 Chunker 之前）
        # 保证 Chunker 失败时迁移为 PROCESSING → FAILED，杜绝 PENDING → FAILED
        # 非法迁移。置位失败直接向上传播，Document 保持 PENDING 不误标 FAILED。
        # ------------------------------------------------------------------
        self._document_repository.update_status(
            document.id,
            DocumentStatus.PROCESSING,
        )

        # ------------------------------------------------------------------
        # 3) Chunker 切分：单独 try/except —— 失败走 PROCESSING → FAILED；
        #    成功则把 chunks 交给 DocumentIngestService（其内部负责后续
        #    状态迁移 / error_message / chunk_count / Milvus ingest，不重复实现）。
        # ------------------------------------------------------------------
        try:
            chunks = self._chunker.split(raw_text)
        except Exception as exc:
            self._mark_failed(document.id, exc)
            raise

        # ------------------------------------------------------------------
        # 4) ingest（Embedding → Milvus Upsert + Stale Delete）；
        #    内部失败由 DocumentIngestService 置 FAILED，异常继续向上传播
        # ------------------------------------------------------------------
        await self._document_ingest_service.ingest_document(
            document.id,
            chunks,
            user_id=user_id,
            api_key=api_key,
        )

        # ------------------------------------------------------------------
        # 5) 返回终态（SUCCESS，error_message=None）
        # ------------------------------------------------------------------
        return self._document_repository.get_document(document.id, user_id)

    def _mark_failed(self, document_id: int, exc: Exception) -> None:
        """
        把 Document 置为 FAILED 并写入截断后的错误摘要（update_failure）。

        本方法**不吞原始异常**：update_failure 自身失败仅记录日志（log + logger），
        原始异常仍由调用方继续向上传播；error_message 截断到
        _MAX_ERROR_MESSAGE_LENGTH 以内，避免异常链超长撑爆 MySQL TEXT。
        """
        summary = str(exc)[:_MAX_ERROR_MESSAGE_LENGTH] or exc.__class__.__name__
        try:
            self._document_repository.update_failure(
                document_id,
                error_message=summary,
            )
        except Exception as write_err:  # noqa: BLE001 - 失败落盘自身出错不能覆盖原异常
            logger.exception(
                "failed to persist FAILED state for web clip document %s "
                "(original error: %s, failure-write error: %s)",
                document_id,
                exc,
                write_err,
            )
