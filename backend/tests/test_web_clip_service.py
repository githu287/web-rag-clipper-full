"""
WebClipService 单元测试（Phase 3.1 Step 3；Phase 3.5 Step 2-E workspace-aware）。

Phase 3.5 Step 2-E 变更（user_id → plugin_id）：
  - clip(url, raw_text, plugin_id, title=None)：plugin_id 必填（显式传入，服务内不生成）；
  - create_document 以 plugin_id=plugin_id 落库；
  - ingest_document(document.id, chunks, plugin_id=plugin_id)；
  - get_document(document.id, plugin_id)。

技术栈：unittest + unittest.mock（Mock(spec=...)），不依赖真实 MySQL/Milvus/百炼。

覆盖用例（对应 Step 3 §十 测试要求 1~7）：
  1. 成功：create_document(PENDING) → update_status(PROCESSING) → chunker.split
     → ingest_document → 返回 SUCCESS 终态。
  2. 状态机顺序：PROCESSING 必须先于 Chunker（顺序哨兵断言）。
  3. Chunker 失败：PROCESSING → FAILED（update_failure + error_message），
     ingest_document 不被调用，原异常继续传播。
  4. filename 固定为 "webclip.txt"。
  5. file_path 固定为 ""。
  6. source_type 固定为 "webpage"。
  7. title=None 正常工作（不传 title 参数 / 传 None）。
  8. url / title / source_type 正确透传给 create_document。
  9. update_failure 自身失败不吞原异常。
  10. create_document 失败直接传播（Document 未创建，不误标 FAILED）。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from backend.chunkers import Chunker
from backend.core.exceptions import (
    DocumentChunkingError,
    DocumentRepositoryError,
)
from backend.models.document import (
    Document,
    DocumentSourceType,
    DocumentStatus,
)
from backend.repositories.mysql import DocumentRepository
from backend.services.document_ingest import DocumentIngestService
from backend.services.web_clip import (
    WebClipService,
    _WEB_CLIP_FILENAME,
    _WEB_CLIP_FILE_PATH,
    _MAX_ERROR_MESSAGE_LENGTH,
)


class WebClipServiceTest(unittest.TestCase):
    """WebClipService 单元测试（Mock 三个依赖）。"""

    def setUp(self) -> None:
        """构造注入依赖：Mock(spec=Protocol/Service)。"""
        self.document_repo = Mock(spec=DocumentRepository)
        self.chunker = Mock(spec=Chunker)
        self.ingest_service = Mock(spec=DocumentIngestService)

        # async 方法
        self.ingest_service.ingest_document = AsyncMock(return_value=None)

        # 默认成功路径返回值
        self.chunker.split.return_value = ["chunk-1", "chunk-2"]

        self.service = WebClipService(
            document_repository=self.document_repo,
            chunker=self.chunker,
            document_ingest_service=self.ingest_service,
        )

    def run_async(self, coro) -> object:
        """同步运行 async 测试体。"""
        return asyncio.run(coro)

    # ------------------------------------------------------------- helpers
    def _make_document(
        self,
        doc_id: int = 1,
        filename: str = _WEB_CLIP_FILENAME,
        file_path: str = _WEB_CLIP_FILE_PATH,
        status: str = DocumentStatus.PENDING,
        chunk_count: int = 0,
        error_message: str | None = None,
        title: str | None = None,
        url: str | None = None,
        source_type: str = DocumentSourceType.WEBPAGE,
    ) -> Mock:
        """构造带全部响应字段的 Mock Document。"""
        doc = Mock()
        doc.id = doc_id
        doc.filename = filename
        doc.file_path = file_path
        doc.status = status
        doc.chunk_count = chunk_count
        doc.error_message = error_message
        doc.title = title
        doc.url = url
        doc.source_type = source_type
        return doc

    # ------------------------------------------------- 1. 成功链路 + 状态机顺序
    def test_clip_success_full_chain(self) -> None:
        """1：成功走 create(PENDING) → PROCESSING → split → ingest → SUCCESS。"""
        pending = self._make_document(status=DocumentStatus.PENDING)
        success = self._make_document(
            status=DocumentStatus.SUCCESS,
            chunk_count=2,
            error_message=None,
            title="示例文章",
            url="https://example.com/article/1",
        )
        self.document_repo.create_document.return_value = pending
        self.document_repo.get_document.return_value = success

        result = self.run_async(
            self.service.clip(
                url="https://example.com/article/1",
                raw_text="网页正文纯文本",
                plugin_id="plugin-a",
                title="示例文章",
            )
        )

        # 1) create_document：固定 filename / file_path / source_type；透传 title / url
        self.document_repo.create_document.assert_called_once_with(
            filename=_WEB_CLIP_FILENAME,
            file_path=_WEB_CLIP_FILE_PATH,
            plugin_id="plugin-a",
            title="示例文章",
            url="https://example.com/article/1",
            source_type=DocumentSourceType.WEBPAGE,
        )
        # 2) PROCESSING 在 Chunker 之前
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING
        )
        self.chunker.split.assert_called_once_with("网页正文纯文本")
        self.ingest_service.ingest_document.assert_awaited_once_with(
            1, ["chunk-1", "chunk-2"], plugin_id="plugin-a", api_key=None
        )
        # 3) 终态读取（workspace-aware）
        self.document_repo.get_document.assert_called_once_with(1, "plugin-a")
        self.assertIs(result, success)
        # 4) 失败路径未触发
        self.document_repo.update_failure.assert_not_called()

    def test_state_machine_processing_before_chunker(self) -> None:
        """2：状态机顺序 —— split 执行时 PROCESSING 必须已置位（顺序哨兵）。"""
        pending = self._make_document(status=DocumentStatus.PENDING)
        success = self._make_document(
            status=DocumentStatus.SUCCESS, chunk_count=2, error_message=None
        )
        self.document_repo.create_document.return_value = pending
        self.document_repo.get_document.return_value = success

        def splitting(text: str) -> list[str]:
            # 顺序哨兵：PROCESSING 置位必须已经发生
            self.document_repo.update_status.assert_called_once_with(
                1, DocumentStatus.PROCESSING
            )
            return ["chunk-1", "chunk-2"]

        self.chunker.split.side_effect = splitting

        result = self.run_async(
            self.service.clip(
                url="https://example.com/a",
                raw_text="hello world",
                plugin_id="plugin-a",
            )
        )

        self.assertEqual(result.status, DocumentStatus.SUCCESS)
        # 状态机：create(PENDING) → update_status(PROCESSING) → split → ingest
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING
        )
        self.document_repo.create_document.assert_called_once()
        self.ingest_service.ingest_document.assert_awaited_once_with(
            1, ["chunk-1", "chunk-2"], plugin_id="plugin-a", api_key=None
        )

    # ---------------------------------------------- 3. Chunker 失败（状态机回归）
    def test_chunker_failure_marks_failed_and_propagates(self) -> None:
        """3：split 抛 DocumentChunkingError → FAILED + error_message，ingest 不调用。"""
        original_error = DocumentChunkingError("split failed")
        self.chunker.split.side_effect = original_error
        self.document_repo.create_document.return_value = self._make_document(
            status=DocumentStatus.PENDING
        )

        with self.assertRaises(DocumentChunkingError) as cm:
            self.run_async(
                self.service.clip(
                    url="https://example.com/a",
                    raw_text="hello world",
                    plugin_id="plugin-a",
                )
            )

        self.assertIs(cm.exception, original_error)
        # PROCESSING 先置位（杜绝 PENDING → FAILED）
        self.document_repo.update_status.assert_called_once_with(
            1, DocumentStatus.PROCESSING
        )
        # update_failure 落 FAILED + error_message
        self.document_repo.update_failure.assert_called_once_with(
            1, error_message="split failed"
        )
        # ingest 不调用
        self.ingest_service.ingest_document.assert_not_called()
        # 失败不读取终态
        self.document_repo.get_document.assert_not_called()

    def test_chunker_failure_keeps_chunk_count_old_value(self) -> None:
        """3：Chunker 失败时 chunk_count 保持旧值（不调用 update_ingest_result）。"""
        self.chunker.split.side_effect = DocumentChunkingError("boom")
        self.document_repo.create_document.return_value = self._make_document(
            status=DocumentStatus.PENDING, chunk_count=0
        )

        with self.assertRaises(DocumentChunkingError):
            self.run_async(
                self.service.clip(
                    url="https://example.com/a",
                    raw_text="hello world",
                    plugin_id="plugin-a",
                )
            )

        # chunk_count 只可能被 update_ingest_result 修改；失败路径禁止调用
        self.document_repo.update_ingest_result.assert_not_called()
        self.document_repo.update_failure.assert_called_once()

    def test_chunker_failure_error_message_truncated(self) -> None:
        """3：超长异常消息截断到 _MAX_ERROR_MESSAGE_LENGTH。"""
        self.chunker.split.side_effect = DocumentChunkingError("x" * 5000)
        self.document_repo.create_document.return_value = self._make_document()

        with self.assertRaises(DocumentChunkingError):
            self.run_async(
                self.service.clip(
                    url="https://example.com/a",
                    raw_text="hello world",
                    plugin_id="plugin-a",
                )
            )

        msg = self.document_repo.update_failure.call_args.kwargs["error_message"]
        self.assertLessEqual(len(msg), _MAX_ERROR_MESSAGE_LENGTH)

    # ----------------------------------------- 4/5/6. filename / file_path / source_type
    def test_filename_filepath_source_type_constants(self) -> None:
        """4/5/6：create_document 固定 filename=webclip.txt / file_path="" / source_type=webpage。"""
        pending = self._make_document()
        self.document_repo.create_document.return_value = pending
        self.document_repo.get_document.return_value = self._make_document(
            status=DocumentStatus.SUCCESS, chunk_count=1, error_message=None
        )

        self.run_async(
            self.service.clip(
                url="https://example.com/a",
                raw_text="hello",
                plugin_id="plugin-a",
            )
        )

        kwargs = self.document_repo.create_document.call_args.kwargs
        self.assertEqual(kwargs["filename"], _WEB_CLIP_FILENAME)
        self.assertEqual(kwargs["filename"], "webclip.txt")
        self.assertEqual(kwargs["file_path"], _WEB_CLIP_FILE_PATH)
        self.assertEqual(kwargs["file_path"], "")
        self.assertEqual(kwargs["source_type"], DocumentSourceType.WEBPAGE)
        self.assertEqual(kwargs["source_type"], "webpage")

    def test_filename_never_derived_from_title_or_url(self) -> None:
        """4：filename 固定为 webclip.txt，禁止用 title / URL 派生。"""
        pending = self._make_document()
        self.document_repo.create_document.return_value = pending
        self.document_repo.get_document.return_value = self._make_document(
            status=DocumentStatus.SUCCESS, chunk_count=1, error_message=None
        )

        self.run_async(
            self.service.clip(
                url="https://example.com/article/123",
                raw_text="hello",
                plugin_id="plugin-a",
                title="我的文章标题",
            )
        )

        kwargs = self.document_repo.create_document.call_args.kwargs
        self.assertEqual(kwargs["filename"], "webclip.txt")
        # title / url 只进入对应列，不进入 filename
        self.assertEqual(kwargs["title"], "我的文章标题")
        self.assertEqual(kwargs["url"], "https://example.com/article/123")

    # -------------------------------------------------------- 7. title=None 兼容
    def test_title_none_works(self) -> None:
        """7：title=None 正常：不传 title 参数与显式传 None 均工作。"""
        pending = self._make_document(title=None, url="https://example.com/a")
        success = self._make_document(
            status=DocumentStatus.SUCCESS,
            chunk_count=1,
            error_message=None,
            title=None,
            url="https://example.com/a",
        )
        self.document_repo.create_document.return_value = pending
        self.document_repo.get_document.return_value = success

        # 不传 title 参数（默认 None）
        result = self.run_async(
            self.service.clip(
                url="https://example.com/a",
                raw_text="hello",
                plugin_id="plugin-a",
            )
        )
        self.assertEqual(
            self.document_repo.create_document.call_args.kwargs["title"], None
        )
        self.assertEqual(result.status, DocumentStatus.SUCCESS)

        # 显式传 title=None
        self.document_repo.reset_mock()
        self.document_repo.create_document.return_value = pending
        self.document_repo.get_document.return_value = success
        self.run_async(
            self.service.clip(
                url="https://example.com/a",
                raw_text="hello",
                plugin_id="plugin-a",
                title=None,
            )
        )
        self.assertEqual(
            self.document_repo.create_document.call_args.kwargs["title"], None
        )

    # --------------------------------------------------- 9. update_failure 自身失败
    def test_update_failure_error_does_not_swallow_original(self) -> None:
        """9：update_failure 抛异常 → 仅记日志，原异常仍继续传播。"""
        original_error = DocumentChunkingError("split failed")
        self.chunker.split.side_effect = original_error
        self.document_repo.create_document.return_value = self._make_document()
        self.document_repo.update_failure.side_effect = RuntimeError("mysql down")

        with self.assertRaises(DocumentChunkingError) as cm:
            self.run_async(
                self.service.clip(
                    url="https://example.com/a",
                    raw_text="hello world",
                    plugin_id="plugin-a",
                )
            )
        # 抛出的仍是原始 Chunker 异常（不被 update_failure 覆盖）
        self.assertIs(cm.exception, original_error)

    # ------------------------------------------- 10. create_document 失败直接传播
    def test_create_document_failure_propagates(self) -> None:
        """10：create_document 抛异常 → 直接传播，不误标 FAILED。"""
        original_error = DocumentRepositoryError("mysql down")
        self.document_repo.create_document.side_effect = original_error

        with self.assertRaises(DocumentRepositoryError) as cm:
            self.run_async(
                self.service.clip(
                    url="https://example.com/a",
                    raw_text="hello world",
                    plugin_id="plugin-a",
                )
            )
        self.assertIs(cm.exception, original_error)
        # Document 未创建成功 → 不写 FAILED
        self.document_repo.update_status.assert_not_called()
        self.document_repo.update_failure.assert_not_called()
        self.chunker.split.assert_not_called()
        self.ingest_service.ingest_document.assert_not_called()

    # ----------------------------------------------------- 附加：Protocol 注入
    def test_protocol_injection(self) -> None:
        """附加：依赖以 Mock(spec=...) 注入，未声明方法访问抛 AttributeError。"""
        with self.assertRaises(AttributeError):
            _ = self.document_repo.not_a_real_method
        with self.assertRaises(AttributeError):
            _ = self.chunker.not_a_real_method


if __name__ == "__main__":
    unittest.main()
