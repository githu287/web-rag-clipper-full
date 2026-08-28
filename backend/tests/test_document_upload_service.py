"""
DocumentUploadService 单元测试（Phase 2.10 Step 3；Phase 3.5 Step 2-E workspace-aware）。

Phase 3.5 Step 2-E 变更（user_id → plugin_id）：
  - upload(..., plugin_id) 必填（归属字段，由认证上下文 / 测试显式传入）；
  - create_document 必须显式带 plugin_id；
  - ingest_document(document.id, chunks, plugin_id=plugin_id)；
  - get_document(document.id, plugin_id)。

技术栈：unittest + unittest.mock（不引入 pytest；不依赖真实 MySQL/Milvus/百炼/磁盘）。
注入方式：Mock(spec=...) 覆盖 Protocol 与 Service，保证：
  - 通过 Protocol runtime_checkable 校验；
  - 断言调用顺序与边界行为（不调用、不落盘、不吞异常）。

覆盖场景（对应 Step 3 §十七 Service 测试要求）：
  A. 正常 .txt 上传 → save(逻辑路径) → create(PENDING) → resolve(物理路径)
     → parse → split → ingest → 终态 SUCCESS
  B. 正常 .md 上传（扩展名分支）
  C. 文件超限 → DocumentFileTooLargeError，不 save 不 create
  D. 空文件（0 字节）→ DocumentFileEmptyError，不 save 不 create
  E. 不支持扩展名（.pdf）→ DocumentUnsupportedExtensionError，不 save 不 create
  F. 空文件名 / 含路径分隔符 → DocumentUploadError
  G. Storage 失败 → 原异常传播，create / parse / split / ingest 均不调用
  H. Parser 失败 → update_failure(FAILED + error_message)，ingest 不调用，原异常传播
  I. Chunker 失败 → update_failure(FAILED + error_message)，ingest 不调用，原异常传播
  J. split 返回空 → DocumentUploadError → update_failure + 原异常传播
  K. ingest 失败 → update_failure(FAILED + error_message)，原异常传播
  L. error_message 截断 ≤ _MAX_ERROR_MESSAGE_LENGTH（2048）
  M. 成功：status=SUCCESS、chunk_count 正确、error_message=None
  N. 文件保留策略：失败路径不调用 FileStorage.delete（保留文件便于 retry）
  O. update_failure 自身失败 → 仅记日志，原异常继续传播（不吞）

Phase 2.10 Step 3.2 状态机修复回归（P1：PENDING→FAILED 非法迁移）：
  P. Parser 失败 / Chunker 失败 / 空 chunks → create(PENDING) → PROCESSING → FAILED
  Q. Embedding/Milvus 失败（ingest_document 抛异常）→ PROCESSING → FAILED
  R. 成功 → create(PENDING) → PROCESSING → SUCCESS
  共性断言：update_status(PROCESSING) 先于 Parser/Chunker/ingest 发生；
  失败路径不调用 update_ingest_result（chunk_count 不被错误修改）；原异常传播。
  S. update_status(PROCESSING) 自身失败 → 原异常直接传播，不误标 FAILED。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from backend.chunkers import Chunker
from backend.core.exceptions import (
    DocumentChunkingError,
    DocumentFileEmptyError,
    DocumentFileTooLargeError,
    DocumentParserError,
    DocumentRepositoryError,
    DocumentStorageError,
    DocumentUnsupportedExtensionError,
    DocumentUploadError,
)
from backend.models.document import DocumentStatus
from backend.parsers import DocumentParser
from backend.repositories.mysql import DocumentRepository
from backend.services.document_ingest import DocumentIngestService
from backend.services.document_upload import (
    DocumentUploadService,
    _MAX_ERROR_MESSAGE_LENGTH,
    _SUPPORTED_EXTENSIONS,
)
from backend.storage import FileStorage


class DocumentUploadServiceTest(unittest.TestCase):
    """DocumentUploadService 单元测试（unittest + mock）。"""

    def setUp(self) -> None:
        """构造注入依赖：Mock(spec=Protocol/Service)，max_content_bytes=100。"""
        self.document_repo = Mock(spec=DocumentRepository)
        self.file_storage = Mock(spec=FileStorage)
        self.parser = Mock(spec=DocumentParser)
        self.chunker = Mock(spec=Chunker)
        self.ingest_service = Mock(spec=DocumentIngestService)

        # async 方法
        self.ingest_service.ingest_document = AsyncMock(return_value=None)

        # 默认成功路径返回值
        # save 返回逻辑路径（相对 upload_dir，裸文件名）；resolve 将其转换为
        # Parser 可直接读取的物理路径（契约：Parser 收到 resolve 结果而非裸逻辑路径）
        self.file_storage.save.return_value = "abc.txt"
        self.file_storage.resolve.return_value = "uploads/txt/abc.txt"
        self.parser.parse.return_value = "hello world"
        self.chunker.split.return_value = ["hello world"]

        self.service = DocumentUploadService(
            document_repository=self.document_repo,
            file_storage=self.file_storage,
            parser=self.parser,
            chunker=self.chunker,
            document_ingest_service=self.ingest_service,
            max_content_bytes=100,
        )

    def run_async(self, coro) -> object:
        """同步运行 async 测试体。"""
        return asyncio.run(coro)

    # ------------------------------------------------------------- helpers
    def _make_document(
        self,
        doc_id: int = 1,
        filename: str = "test.txt",
        file_path: str = "abc.txt",
        status: str = DocumentStatus.PENDING,
        chunk_count: int = 0,
        file_size: int = 11,
        mime_type: str = "text/plain",
        error_message: str | None = None,
    ) -> Mock:
        """构造带全部响应字段的 Mock Document。"""
        doc = Mock()
        doc.id = doc_id
        doc.filename = filename
        doc.file_path = file_path
        doc.status = status
        doc.chunk_count = chunk_count
        doc.file_size = file_size
        doc.mime_type = mime_type
        doc.error_message = error_message
        return doc

    def _assert_failure_state_written(self, error_summary: str) -> None:
        """断言失败路径调用了 update_failure(document_id, error_message=...)。"""
        self.document_repo.update_failure.assert_called_once()
        call = self.document_repo.update_failure.call_args
        self.assertEqual(call.args[0], 1)
        self.assertIn("error_message", call.kwargs)
        self.assertEqual(call.kwargs["error_message"], error_summary)

    # ------------------------------------------------- A. 正常 .txt 上传
    def test_upload_txt_success(self) -> None:
        """A：正常 .txt 上传走完整链路并返回 SUCCESS 终态。"""
        pending = self._make_document(status=DocumentStatus.PENDING, chunk_count=0)
        success = self._make_document(
            status=DocumentStatus.SUCCESS, chunk_count=1, error_message=None
        )
        self.document_repo.create_document.return_value = pending
        self.document_repo.get_document.return_value = success

        result = self.run_async(
            self.service.upload(
                filename="test.txt",
                content=b"hello world",
                plugin_id="plugin-a",
                mime_type="text/plain",
            )
        )

        # 1) 落盘 → 创建 → 解析 → 切分 → ingest 调用链
        self.file_storage.save.assert_called_once_with(
            "test.txt", b"hello world"
        )
        self.document_repo.create_document.assert_called_once_with(
            filename="test.txt",
            file_path="abc.txt",
            plugin_id="plugin-a",
            file_size=11,
            mime_type="text/plain",
        )
        # Document.file_path 保存的是 save() 返回的逻辑路径（裸文件名），
        # 而非带 upload_dir 前缀的路径
        self.assertEqual(
            self.document_repo.create_document.call_args.kwargs["file_path"],
            "abc.txt",
        )
        # Parser 收到的是 resolve() 返回的物理路径，而非 save() 返回的裸逻辑路径
        self.file_storage.resolve.assert_called_once_with("abc.txt")
        self.parser.parse.assert_called_once_with(
            self.file_storage.resolve.return_value
        )
        self.chunker.split.assert_called_once_with("hello world")
        self.ingest_service.ingest_document.assert_awaited_once_with(
            1, ["hello world"], plugin_id="plugin-a", api_key=None
        )
        # 2) 终态读取（workspace-aware）
        self.document_repo.get_document.assert_called_once_with(1, "plugin-a")
        # 3) 返回 SUCCESS 对象（error_message=None）
        self.assertIs(result, success)
        self.assertEqual(result.status, DocumentStatus.SUCCESS)
        self.assertEqual(result.chunk_count, 1)
        self.assertIsNone(result.error_message)
        # 4) 失败路径未被触发
        self.document_repo.update_failure.assert_not_called()

    # ------------------------------------------------- B. 正常 .md 上传
    def test_upload_markdown_success(self) -> None:
        """B：.md 与 .markdown 扩展名同样支持。"""
        success = self._make_document(
            filename="note.md",
            status=DocumentStatus.SUCCESS,
            chunk_count=2,
            error_message=None,
        )
        self.document_repo.create_document.return_value = self._make_document(
            filename="note.md"
        )
        self.document_repo.get_document.return_value = success

        result = self.run_async(
            self.service.upload(
                filename="note.md",
                content=b"# title\n\nbody",
                plugin_id="plugin-7",
                mime_type="text/markdown",
            )
        )

        self.file_storage.save.assert_called_once_with(
            "note.md", b"# title\n\nbody"
        )
        self.document_repo.create_document.assert_called_once_with(
            filename="note.md",
            file_path="abc.txt",
            plugin_id="plugin-7",
            file_size=13,
            mime_type="text/markdown",
        )
        self.assertEqual(result.status, DocumentStatus.SUCCESS)
        self.assertEqual(result.chunk_count, 2)

    def test_upload_markdown_extension_variants(self) -> None:
        """B：.markdown 也在支持集合内（大小写不敏感）。"""
        self.assertIn(".markdown", _SUPPORTED_EXTENSIONS)
        success = self._make_document(
            filename="README.markdown",
            status=DocumentStatus.SUCCESS,
            chunk_count=1,
        )
        self.document_repo.create_document.return_value = self._make_document(
            filename="README.markdown"
        )
        self.document_repo.get_document.return_value = success

        result = self.run_async(
            self.service.upload(
                filename="README.markdown",
                content=b"# readme",
                plugin_id="plugin-a",
                mime_type=None,
            )
        )

        self.assertEqual(result.status, DocumentStatus.SUCCESS)
        # mime_type 为 None → 落库空串
        self.document_repo.create_document.assert_called_once()
        self.assertEqual(
            self.document_repo.create_document.call_args.kwargs["mime_type"], ""
        )

    # ------------------------------------------------- C. 文件超限
    def test_upload_file_too_large(self) -> None:
        """C：len(content) > max_content_bytes → FileTooLarge，不 save 不 create。"""
        with self.assertRaises(DocumentFileTooLargeError):
            self.run_async(
                self.service.upload(
                    filename="big.txt",
                    content=b"x" * 101,
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.file_storage.save.assert_not_called()
        self.document_repo.create_document.assert_not_called()
        self.parser.parse.assert_not_called()
        self.chunker.split.assert_not_called()
        self.ingest_service.ingest_document.assert_not_called()

    # ------------------------------------------------- D. 空文件
    def test_upload_empty_file(self) -> None:
        """D：0 字节文件 → FileEmpty，不 save 不 create（禁止空 SUCCESS）。"""
        with self.assertRaises(DocumentFileEmptyError):
            self.run_async(
                self.service.upload(
                    filename="empty.txt",
                    content=b"",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.file_storage.save.assert_not_called()
        self.document_repo.create_document.assert_not_called()

    # ------------------------------------------------- E. 不支持扩展名
    def test_upload_unsupported_extension(self) -> None:
        """E：.pdf → UnsupportedExtension，不 save 不 create。"""
        with self.assertRaises(DocumentUnsupportedExtensionError):
            self.run_async(
                self.service.upload(
                    filename="report.pdf",
                    content=b"%PDF-1.4",
                    plugin_id="plugin-a",
                    mime_type="application/pdf",
                )
            )
        self.file_storage.save.assert_not_called()
        self.document_repo.create_document.assert_not_called()

    def test_upload_unsupported_extension_docx(self) -> None:
        """E：.docx 同样明确拒绝。"""
        with self.assertRaises(DocumentUnsupportedExtensionError):
            self.run_async(
                self.service.upload(
                    filename="doc.docx",
                    content=b"PK",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.file_storage.save.assert_not_called()

    # ------------------------------------------------- F. 文件名非法
    def test_upload_empty_filename(self) -> None:
        """F：空文件名 → DocumentUploadError，不 save 不 create。"""
        with self.assertRaises(DocumentUploadError):
            self.run_async(
                self.service.upload(
                    filename="",
                    content=b"hello",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.file_storage.save.assert_not_called()

    def test_upload_filename_with_separator_rejected(self) -> None:
        """F：含 `/` 或 `\\` 的文件名 → DocumentUploadError（路径穿越入口拦截）。"""
        for bad_name in ("../evil.txt", "..\\evil.txt", "a/b.txt", "a\\b.txt"):
            with self.subTest(filename=bad_name):
                with self.assertRaises(DocumentUploadError):
                    self.run_async(
                        self.service.upload(
                            filename=bad_name,
                            content=b"hello",
                            plugin_id="plugin-a",
                            mime_type=None,
                        )
                    )
                self.file_storage.save.assert_not_called()

    # ------------------------------------------------- G. Storage 失败
    def test_storage_failure_propagates(self) -> None:
        """G：save 抛 DocumentStorageError → 原异常传播，后续步骤全部不调用。"""
        original_error = DocumentStorageError("disk full")
        self.file_storage.save.side_effect = original_error

        with self.assertRaises(DocumentStorageError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.assertIs(cm.exception, original_error)
        # Document 尚未创建：不写 FAILED
        self.document_repo.create_document.assert_not_called()
        self.document_repo.update_failure.assert_not_called()
        self.parser.parse.assert_not_called()
        self.chunker.split.assert_not_called()
        self.ingest_service.ingest_document.assert_not_called()

    # ------------------------------------------------- H. Parser 失败
    def test_parser_failure_marks_failed_and_propagates(self) -> None:
        """H：parse 抛 DocumentParserError → FAILED + error_message，ingest 不调用。"""
        original_error = DocumentParserError("read failed")
        self.parser.parse.side_effect = original_error
        self.document_repo.create_document.return_value = self._make_document()

        with self.assertRaises(DocumentParserError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.assertIs(cm.exception, original_error)
        self._assert_failure_state_written("read failed")
        self.chunker.split.assert_not_called()
        self.ingest_service.ingest_document.assert_not_called()

    # ------------------------------------------------- I. Chunker 失败
    def test_chunker_failure_marks_failed_and_propagates(self) -> None:
        """I：split 抛 DocumentChunkingError → FAILED + error_message，ingest 不调用。"""
        original_error = DocumentChunkingError("split failed")
        self.chunker.split.side_effect = original_error
        self.document_repo.create_document.return_value = self._make_document()

        with self.assertRaises(DocumentChunkingError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.assertIs(cm.exception, original_error)
        self._assert_failure_state_written("split failed")
        self.ingest_service.ingest_document.assert_not_called()

    # ------------------------------------------------- J. split 返回空
    def test_empty_chunks_marks_failed(self) -> None:
        """J：split 返回 []（纯空白文本）→ DocumentUploadError → FAILED + 传播。"""
        self.chunker.split.return_value = []
        self.document_repo.create_document.return_value = self._make_document()

        with self.assertRaises(DocumentUploadError):
            self.run_async(
                self.service.upload(
                    filename="blank.txt",
                    content=b"   ",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.document_repo.update_failure.assert_called_once()
        self.assertIn(
            "no chunks produced",
            self.document_repo.update_failure.call_args.kwargs["error_message"],
        )
        self.ingest_service.ingest_document.assert_not_called()

    # ------------------------------------------------- K. ingest 失败
    def test_ingest_failure_marks_failed_and_propagates(self) -> None:
        """K：ingest_document 抛异常 → FAILED + error_message，原异常传播。"""
        original_error = RuntimeError("milvus upsert failed")
        self.ingest_service.ingest_document.side_effect = original_error
        self.document_repo.create_document.return_value = self._make_document()

        with self.assertRaises(RuntimeError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.assertIs(cm.exception, original_error)
        self._assert_failure_state_written("milvus upsert failed")
        # 失败路径不读取终态
        self.document_repo.get_document.assert_not_called()

    # ------------------------------------------------- L. error_message 截断
    def test_error_message_truncated(self) -> None:
        """L：超长异常消息截断到 _MAX_ERROR_MESSAGE_LENGTH。"""
        self.parser.parse.side_effect = DocumentParserError("x" * 5000)
        self.document_repo.create_document.return_value = self._make_document()

        with self.assertRaises(DocumentParserError):
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        msg = self.document_repo.update_failure.call_args.kwargs["error_message"]
        self.assertLessEqual(len(msg), _MAX_ERROR_MESSAGE_LENGTH)

    def test_error_message_empty_str_falls_back_to_class_name(self) -> None:
        """L：空异常消息回退为异常类名。"""
        self.parser.parse.side_effect = DocumentParserError("")
        self.document_repo.create_document.return_value = self._make_document()

        with self.assertRaises(DocumentParserError):
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        msg = self.document_repo.update_failure.call_args.kwargs["error_message"]
        self.assertEqual(msg, "DocumentParserError")

    # ------------------------------------------------- N. 文件保留策略
    def test_failed_path_keeps_file(self) -> None:
        """N：Document 创建后的失败路径**不删除**已保存文件（保留便于 retry）。"""
        self.parser.parse.side_effect = DocumentParserError("read failed")
        self.document_repo.create_document.return_value = self._make_document()

        with self.assertRaises(DocumentParserError):
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        # 文件已落盘且未删除
        self.file_storage.save.assert_called_once()
        self.file_storage.delete.assert_not_called()
        # FAILED 状态落库
        self.document_repo.update_failure.assert_called_once()

    # ------------------------------------------------- O. update_failure 自身失败
    def test_update_failure_error_does_not_swallow_original(self) -> None:
        """O：update_failure 抛异常 → 仅记日志，原始异常仍继续传播。"""
        original_error = DocumentParserError("read failed")
        self.parser.parse.side_effect = original_error
        self.document_repo.create_document.return_value = self._make_document()
        self.document_repo.update_failure.side_effect = RuntimeError(
            "mysql down"
        )

        with self.assertRaises(DocumentParserError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        # 抛出的仍是原始 Parser 异常（不被 update_failure 覆盖）
        self.assertIs(cm.exception, original_error)

    # ------------------------------------------------- 附加：Protocol 注入
    def test_protocol_injection(self) -> None:
        """附加：依赖以 Mock(spec=...) 注入，未声明方法访问抛 AttributeError。"""
        with self.assertRaises(AttributeError):
            _ = self.document_repo.not_a_real_method
        with self.assertRaises(AttributeError):
            _ = self.file_storage.not_a_real_method
        with self.assertRaises(AttributeError):
            _ = self.parser.not_a_real_method
        with self.assertRaises(AttributeError):
            _ = self.chunker.not_a_real_method

    # ================================================================
    # Phase 2.10 Step 3.2：状态机修复回归测试（P1：PENDING→FAILED）
    # 目标流程：create(PENDING) → update_status(PROCESSING) → Parser
    #           → Chunker → ingest_document → SUCCESS / FAILED
    # 关键约束：update_status(PROCESSING) 必须先于 Parser / Chunker / ingest；
    #           Parser / Chunker / 空 chunks / Embedding / Milvus 失败
    #           = PROCESSING → FAILED（合法迁移）；
    #           chunk_count 不被错误修改（禁止调用 update_ingest_result）；
    #           原异常继续向上传播。
    # ================================================================

    def test_state_machine_parser_failure(self) -> None:
        """A：Parser 失败 → create(PENDING) → PROCESSING → FAILED（无 PENDING→FAILED）。"""
        original_error = DocumentParserError("read failed")

        def failing_parse(file_path: str) -> str:
            # 顺序哨兵：PROCESSING 置位必须已经发生
            self.document_repo.update_status.assert_called_once_with(
                1, DocumentStatus.PROCESSING
            )
            raise original_error

        self.parser.parse.side_effect = failing_parse
        self.document_repo.create_document.return_value = self._make_document(
            status=DocumentStatus.PENDING
        )

        with self.assertRaises(DocumentParserError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.assertIs(cm.exception, original_error)
        # create(PENDING) → update_status(PROCESSING) → Parser(失败)
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING
        )
        # 失败落盘：FAILED + error_message
        self._assert_failure_state_written("read failed")
        # chunk_count 不被错误修改：禁止走 update_ingest_result
        self.document_repo.update_ingest_result.assert_not_called()
        # 后续步骤不执行
        self.chunker.split.assert_not_called()
        self.ingest_service.ingest_document.assert_not_called()

    def test_state_machine_chunker_failure(self) -> None:
        """B：Chunker 失败 → create(PENDING) → PROCESSING → FAILED（无 PENDING→FAILED）。"""
        original_error = DocumentChunkingError("split failed")

        def failing_split(text: str) -> list[str]:
            # 顺序哨兵：PROCESSING 置位必须已经发生
            self.document_repo.update_status.assert_called_once_with(
                1, DocumentStatus.PROCESSING
            )
            raise original_error

        self.chunker.split.side_effect = failing_split
        self.document_repo.create_document.return_value = self._make_document(
            status=DocumentStatus.PENDING
        )

        with self.assertRaises(DocumentChunkingError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.assertIs(cm.exception, original_error)
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING
        )
        self._assert_failure_state_written("split failed")
        self.document_repo.update_ingest_result.assert_not_called()
        self.ingest_service.ingest_document.assert_not_called()

    def test_state_machine_empty_chunks(self) -> None:
        """C：空 chunks → create(PENDING) → PROCESSING → FAILED（无 PENDING→FAILED）。"""
        def empty_split(text: str) -> list[str]:
            # 顺序哨兵：PROCESSING 置位必须已经发生
            self.document_repo.update_status.assert_called_once_with(
                1, DocumentStatus.PROCESSING
            )
            return []

        self.chunker.split.side_effect = empty_split
        self.document_repo.create_document.return_value = self._make_document(
            status=DocumentStatus.PENDING
        )

        with self.assertRaises(DocumentUploadError):
            self.run_async(
                self.service.upload(
                    filename="blank.txt",
                    content=b"   ",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING
        )
        self._assert_failure_state_written(
            "no chunks produced after parsing (empty or whitespace-only text)"
        )
        self.document_repo.update_ingest_result.assert_not_called()
        self.ingest_service.ingest_document.assert_not_called()

    def test_state_machine_ingest_failure(self) -> None:
        """D：Embedding / Milvus 失败（ingest_document 抛异常）→ PROCESSING → FAILED。"""
        original_error = RuntimeError("milvus upsert failed")

        async def failing_ingest(
            document_id: int,
            chunks: list[str],
            plugin_id: str,
            api_key: str | None = None,  # Phase 3.4 Step 4：api_key 透传
        ) -> None:
            # 顺序哨兵：PROCESSING 置位必须已经发生
            self.document_repo.update_status.assert_called_once_with(
                1, DocumentStatus.PROCESSING
            )
            raise original_error

        self.ingest_service.ingest_document.side_effect = failing_ingest
        self.document_repo.create_document.return_value = self._make_document(
            status=DocumentStatus.PENDING
        )

        with self.assertRaises(RuntimeError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.assertIs(cm.exception, original_error)
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING
        )
        # FAILED + error_message（UploadService 补写；DocumentIngestService 已置 FAILED）
        self._assert_failure_state_written("milvus upsert failed")
        # chunk_count 不被错误修改：禁止走 update_ingest_result
        self.document_repo.update_ingest_result.assert_not_called()

    def test_state_machine_success(self) -> None:
        """E：成功 → create(PENDING) → PROCESSING → SUCCESS；chunk_count 正确。"""
        def parsing(file_path: str) -> str:
            # 顺序哨兵：PROCESSING 置位必须已经发生
            self.document_repo.update_status.assert_called_once_with(
                1, DocumentStatus.PROCESSING
            )
            return "hello world"

        self.parser.parse.side_effect = parsing
        self.document_repo.create_document.return_value = self._make_document(
            status=DocumentStatus.PENDING, chunk_count=0
        )
        self.document_repo.get_document.return_value = self._make_document(
            status=DocumentStatus.SUCCESS, chunk_count=1, error_message=None
        )

        result = self.run_async(
            self.service.upload(
                filename="test.txt",
                content=b"hello world",
                plugin_id="plugin-a",
                mime_type="text/plain",
            )
        )
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING
        )
        self.assertEqual(result.status, DocumentStatus.SUCCESS)
        self.assertEqual(result.chunk_count, 1)
        self.assertIsNone(result.error_message)
        self.document_repo.update_failure.assert_not_called()

    def test_state_machine_processing_set_failure_propagates(self) -> None:
        """F：update_status(PROCESSING) 失败 → 原异常直接传播，不触发 PENDING→FAILED
        （与 DocumentIngestService Step 2 行为一致：置位失败 Document 保持 PENDING）。"""
        original_error = DocumentRepositoryError("mysql down")
        self.document_repo.update_status.side_effect = original_error
        self.document_repo.create_document.return_value = self._make_document(
            status=DocumentStatus.PENDING
        )

        with self.assertRaises(DocumentRepositoryError) as cm:
            self.run_async(
                self.service.upload(
                    filename="test.txt",
                    content=b"hello world",
                    plugin_id="plugin-a",
                    mime_type=None,
                )
            )
        self.assertIs(cm.exception, original_error)
        # 不误标 FAILED（避免 PENDING → FAILED 非法迁移）
        self.document_repo.update_failure.assert_not_called()
        self.parser.parse.assert_not_called()
        self.chunker.split.assert_not_called()
        self.ingest_service.ingest_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
