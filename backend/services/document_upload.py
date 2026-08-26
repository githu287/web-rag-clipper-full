"""
DocumentUploadService（Phase 2.10 Step 3）。

把「HTTP multipart 上传」真正串成完整链路：

    HTTP multipart upload
        ↓
    DocumentUploadService.upload()
        ↓
    输入校验（filename / 空文件 / 大小 / 扩展名）
        ↓
    FileStorage.save()                      # 落盘（相对路径）
        ↓
    create_document(status=PENDING)         # 建 Document（file_size / mime_type）
        ↓
    update_status(PROCESSING)               # Phase 2.10 Step 3.2：立即进入处理中
        ↓
    Parser.parse(resolve(file_path)) → str  # 逻辑路径 → 物理路径 → 完整文本
        ↓
    Chunker.split(text) → list[str]         # 文本 → chunk 列表
        ↓
    DocumentIngestService.ingest_document() # Embedding → Milvus Upsert + Stale Delete
        ↓
    status = SUCCESS，返回终态 Document

失败语义：
1) 输入校验（空文件 / 超限 / 扩展名）在落盘与建 Document **之前**完成，
   不产生任何文件与 Document 行，抛 DocumentUploadError 族（API 映射 4xx）。
2) Document 创建后的任一环节失败：
   - 保留已保存文件（便于 retry 与问题定位，本阶段第一版策略）；
   - Document.status = FAILED + error_message = 截断后的错误摘要（update_failure）；
   - **不吞原始异常**：原异常继续向上传播（API 层按异常类型映射 4xx/5xx）。

设计红线：
- 不直接调用 EmbeddingClient / Milvus（由 DocumentIngestService 内部完成）；
- Parser 只输出 str，Chunker 只接收 str 输出 list[str]，职责不越界；
- file_path 由 FileStorage.save() 生成（相对路径），禁止用 filename 拼接绝对路径；
  Parser 前必须经 FileStorage.resolve(file_path) 将逻辑路径转换为物理路径，
  UploadService 不感知 upload_dir / LocalFileStorage 细节；
- 状态机（Phase 2.10 Step 3.2 修复）：
  create_document(PENDING) → update_status(PROCESSING) → Parser/Chunker/ingest；
  Parser / Chunker / 空 chunks / Embedding / Milvus 任一失败均为合法迁移
  PROCESSING → FAILED；成功为 PROCESSING → SUCCESS；
  不存在 PENDING → FAILED / PENDING → SUCCESS 非法迁移。
  DocumentIngestService 内部再次 update_status(PROCESSING) 属幂等自环
  （既有 POST /documents/{id}/ingest 契约不变，不为此重构）。
"""

from __future__ import annotations

import logging
import os

from ..chunkers import Chunker
from ..core.exceptions import (
    DocumentFileEmptyError,
    DocumentFileTooLargeError,
    DocumentUploadError,
    DocumentUnsupportedExtensionError,
)
from ..models.document import Document, DocumentStatus
from ..parsers import DocumentParser
from ..repositories.mysql import DocumentRepository
from ..storage import FileStorage
from .document_ingest import DocumentIngestService

logger = logging.getLogger(__name__)

# 本阶段支持的上传扩展名（与 parsers/text.py TextDocumentParser 支持集合保持一致；
# 未来若扩展 PDF/DOCX Parser，必须同步维护本集合与 Parser 的 SUPPORTED_EXTENSIONS）。
_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md", ".markdown"})

# error_message 最大长度（MySQL TEXT 上限 64KB；摘要控制在 2KB 足够定位问题）
_MAX_ERROR_MESSAGE_LENGTH = 2048


class DocumentUploadService:
    """
    上传编排服务：输入校验 → 落盘 → 建 Document → 解析 → 切分 → ingest。

    Args:
        document_repository: DocumentRepository（Protocol）——创建 / 查询 / 失败落盘。
        file_storage: FileStorage（Protocol）——原始文件保存，返回相对路径；
            resolve() 将逻辑路径转换为 Parser 可读的物理路径。
        parser: DocumentParser（Protocol）——文件 → str。
        chunker: Chunker（Protocol）——str → list[str]。
        document_ingest_service: DocumentIngestService——Embedding + Milvus 入库。
        max_content_bytes: 单文件大小上限（来自 Settings.max_page_content_bytes）。
    """

    def __init__(
        self,
        document_repository: DocumentRepository,
        file_storage: FileStorage,
        parser: DocumentParser,
        chunker: Chunker,
        document_ingest_service: DocumentIngestService,
        max_content_bytes: int,
    ) -> None:
        self._document_repository = document_repository
        self._file_storage = file_storage
        self._parser = parser
        self._chunker = chunker
        self._document_ingest_service = document_ingest_service
        self._max_content_bytes = max_content_bytes

    async def upload(
        self,
        filename: str,
        content: bytes,
        user_id: int,
        mime_type: str | None,
        api_key: str | None = None,
    ) -> Document:
        """
        执行完整上传链路，返回最终 Document（成功为 SUCCESS，失败抛原异常）。

        校验顺序（全部在 save() / create_document() 之前）：
            1. filename 非空且为纯文件名（不含 `/` `\\`，路径穿越在入口拦截）；
            2. content 非空（0 字节直接拒绝，不创建 Document）；
            3. len(content) <= max_content_bytes（超限直接拒绝，不落盘）；
            4. 扩展名 ∈ {.txt, .md, .markdown}（.pdf / .docx 等返回明确异常）。

        Phase 3.4 Step D：新增 api_key 参数 —— 当前用户的百炼 API Key
        （AuthService 解密后传入），透传至 Embedding；
        Phase 3.4 Step F6：api_key 必填（Client 层已强制），
        严禁回退 settings.bailian_api_key。

        Args:
            filename: 上传文件名（来自 UploadFile.filename，可为空串）。
            content : 文件完整字节内容。
            user_id : 所属用户 ID（**必填**，Phase 3.4 Step C：由认证上下文 /
                      测试显式传入，不允许 None）。
            mime_type: 文件 MIME 类型（来自 UploadFile.content_type，可为 None）。
            api_key: 用户自己的百炼 API Key（Phase 3.4 Step D/F6；必填透传）。

        Returns:
            Document: 终态对象（SUCCESS，chunk_count = 实际入库数，
            error_message = None）。

        Raises:
            DocumentUploadError: 文件名非法。
            DocumentFileEmptyError: 文件内容为空。
            DocumentFileTooLargeError: 文件超过 max_content_bytes。
            DocumentUnsupportedExtensionError: 扩展名不受支持。
            DocumentParserError / DocumentChunkingError / DocumentRepositoryError /
            MilvusRepositoryError / EmbeddingClientError: 链路失败（Document 已置
            FAILED + error_message，原异常继续传播）。
        """
        # ------------------------------------------------------------------
        # 1) 输入校验：全部在落盘与建 Document 之前，失败不产生任何副作用
        # ------------------------------------------------------------------
        if not filename or "/" in filename or "\\" in filename:
            raise DocumentUploadError(
                f"invalid filename: {filename!r} "
                "(must be a plain file name without path separators)"
            )

        if content == b"":
            raise DocumentFileEmptyError("uploaded file is empty (0 bytes)")

        if len(content) > self._max_content_bytes:
            raise DocumentFileTooLargeError(
                f"file size {len(content)} bytes exceeds limit "
                f"{self._max_content_bytes} bytes"
            )

        extension = os.path.splitext(filename)[1].lower()
        if extension not in _SUPPORTED_EXTENSIONS:
            raise DocumentUnsupportedExtensionError(
                f"unsupported file extension: {extension!r}, "
                f"supported: {sorted(_SUPPORTED_EXTENSIONS)}"
            )

        # ------------------------------------------------------------------
        # 2) 落盘（LocalFileStorage 安全校验：纯文件名 + upload_dir 边界内）
        # ------------------------------------------------------------------
        file_path = self._file_storage.save(filename, content)

        # ------------------------------------------------------------------
        # 3) 创建 Document(status=PENDING)，携带 file_size / mime_type
        # ------------------------------------------------------------------
        document = self._document_repository.create_document(
            filename=filename,
            file_path=file_path,
            user_id=user_id,
            file_size=len(content),
            mime_type=mime_type or "",
        )

        # ------------------------------------------------------------------
        # 3.1) 立即置 PROCESSING（Phase 2.10 Step 3.2 状态机修复）
        # 保证 Parser / Chunker / 空 chunks 任一失败时迁移为 PROCESSING → FAILED，
        # 杜绝 PENDING → FAILED 非法迁移。置位失败直接向上传播（与
        # DocumentIngestService Step 2 行为一致），Document 保持 PENDING 不误标 FAILED。
        # ------------------------------------------------------------------
        self._document_repository.update_status(
            document.id,
            DocumentStatus.PROCESSING,
        )

        # ------------------------------------------------------------------
        # 4) 解析 → 切分 → ingest；任一失败：PROCESSING → FAILED + error_message，不吞异常
        #    Parser 前先 resolve()：save 返回的是相对 upload_dir 的逻辑路径，
        #    需经 FileStorage 解析为物理路径（UploadService 不感知 upload_dir）。
        # ------------------------------------------------------------------
        try:
            text = self._parser.parse(self._file_storage.resolve(file_path))
            chunks = self._chunker.split(text)
            if not chunks:
                raise DocumentUploadError(
                    "no chunks produced after parsing (empty or whitespace-only text)"
                )
            await self._document_ingest_service.ingest_document(
                document.id,
                chunks,
                user_id=user_id,
                api_key=api_key,
            )
        except Exception as exc:
            self._mark_failed(document.id, exc)
            raise

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
                "failed to persist FAILED state for document %s "
                "(original error: %s, failure-write error: %s)",
                document_id,
                exc,
                write_err,
            )
