"""
POST /documents/upload API 集成测试（Phase 2.10 Step 3）。

技术栈：unittest + unittest.mock + FastAPI TestClient（python-multipart 已装）。
不连接真实 MySQL / Milvus / 百炼 / 磁盘：
    - 通过 app.dependency_overrides 将 get_document_upload_service 替换为
      fake upload service（AsyncMock(upload)），聚焦「路由 + HTTP 映射」；
    - 通过 patch("backend.main.get_milvus_initializer") 阻断 lifespan 启动期
      真实 Milvus 连接。

覆盖场景（对应 Step 3 §十七 API 测试要求）：
    1. 201 成功（.txt multipart 上传，完整 schema）
    2. multipart 缺 file → 422（FastAPI 必填校验）
    3. 文件超限（service 抛 DocumentFileTooLargeError）→ 413
    4. 空文件（service 抛 DocumentFileEmptyError）→ 400
    5. .txt 上传成功，filename/mime_type/content 透传正确
    6. .md 上传成功
    7. .pdf（service 抛 DocumentUnsupportedExtensionError）→ 415
    8. Parser 错误 → 500
    9. EmbeddingClientError → 502 / MilvusRepositoryError → 503
    10. response schema 字段完整且无多余字段（extra=forbid 生效）
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_user
from backend.clients.embedding import EmbeddingClientError
from backend.core.di import get_document_upload_service, get_user_service
from backend.core.exceptions import (
    DocumentChunkingError,
    DocumentFileEmptyError,
    DocumentFileTooLargeError,
    DocumentParserError,
    DocumentUnsupportedExtensionError,
    DocumentUploadError,
    MilvusRepositoryError,
)
from backend.main import create_app


class DocumentUploadApiTest(unittest.TestCase):
    """POST /documents/upload API 集成测试。"""

    def setUp(self) -> None:
        """构造隔离 app + fake upload service + 阻断 lifespan Milvus 连接。"""
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_upload_service = Mock()
        self.fake_upload_service.upload = AsyncMock()

        self.app = create_app()
        self.app.dependency_overrides[get_document_upload_service] = (
            lambda: self.fake_upload_service
        )
        # Phase 3.4 Step 4：upload 端点需认证 + 用户 API Key 注入
        self.fake_user = SimpleNamespace(id=1)
        self.fake_user_service = Mock()
        self.fake_user_service.decrypt_api_key = Mock(return_value="sk-test")
        self.app.dependency_overrides[get_current_user] = (
            lambda: self.fake_user
        )
        self.app.dependency_overrides[get_user_service] = (
            lambda: self.fake_user_service
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    def _make_document(
        self,
        doc_id: int = 1,
        filename: str = "test.txt",
        status: str = "SUCCESS",
        chunk_count: int = 3,
        file_size: int = 11,
        mime_type: str = "text/plain",
        error_message: str | None = None,
    ) -> Mock:
        doc = Mock()
        doc.id = doc_id
        doc.filename = filename
        doc.file_size = file_size
        doc.mime_type = mime_type
        doc.status = status
        doc.chunk_count = chunk_count
        doc.error_message = error_message
        return doc

    # ------------------------------------------------------- 1. 201 成功
    def test_upload_success_201(self) -> None:
        """1：正常 multipart 上传 → 201 + 完整 schema。"""
        self.fake_upload_service.upload.return_value = self._make_document()

        response = self.client.post(
            "/documents/upload",
            files={"file": ("test.txt", b"hello world", "text/plain")},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "id": 1,
                "filename": "test.txt",
                "file_size": 11,
                "mime_type": "text/plain",
                "status": "SUCCESS",
                "chunk_count": 3,
                "error_message": None,
            },
        )
        self.fake_upload_service.upload.assert_awaited_once()

    # --------------------------------------------------- 2. 缺 file → 422
    def test_upload_missing_file_rejected(self) -> None:
        """2：multipart 缺少 file 字段 → 422（不触发 service）。"""
        response = self.client.post("/documents/upload")

        self.assertEqual(response.status_code, 422)
        self.fake_upload_service.upload.assert_not_called()

    # ------------------------------------------------------- 3. 超限 → 413
    def test_upload_file_too_large_413(self) -> None:
        """3：service 抛 DocumentFileTooLargeError → 413。"""
        self.fake_upload_service.upload.side_effect = (
            DocumentFileTooLargeError("file too large")
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("big.txt", b"x" * 999, "text/plain")},
        )

        self.assertEqual(response.status_code, 413)
        body = response.json()
        self.assertEqual(body["type"], "DocumentFileTooLargeError")
        self.assertEqual(body["detail"], "file too large")

    # ------------------------------------------------------- 4. 空文件 → 400
    def test_upload_empty_file_400(self) -> None:
        """4：service 抛 DocumentFileEmptyError → 400。"""
        self.fake_upload_service.upload.side_effect = (
            DocumentFileEmptyError("uploaded file is empty (0 bytes)")
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("empty.txt", b"", "text/plain")},
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["type"], "DocumentFileEmptyError")

    # ------------------------------------------- 5. .txt 参数透传正确
    def test_upload_txt_passthrough(self) -> None:
        """5：.txt 上传 → upload(filename, content, user_id, mime_type) 透传正确。"""
        self.fake_upload_service.upload.return_value = self._make_document()

        response = self.client.post(
            "/documents/upload",
            files={"file": ("notes.txt", b"line1\nline2", "text/plain")},
            data={"user_id": "42"},
        )

        self.assertEqual(response.status_code, 201)
        self.fake_upload_service.upload.assert_awaited_once_with(
            filename="notes.txt",
            content=b"line1\nline2",
            user_id=1,  # Phase 3.4 Step 4：归属 = current_user.id + 用户 Key 注入
            mime_type="text/plain",
            api_key="sk-test",
        )

    # ------------------------------------------------------- 6. .md 成功
    def test_upload_md_success(self) -> None:
        """6：.md 上传成功（mime text/markdown）。"""
        self.fake_upload_service.upload.return_value = self._make_document(
            filename="doc.md", mime_type="text/markdown", chunk_count=1
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("doc.md", b"# title", "text/markdown")},
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["filename"], "doc.md")
        self.assertEqual(body["mime_type"], "text/markdown")
        self.assertEqual(body["status"], "SUCCESS")

    # ------------------------------------------------------- 7. .pdf → 415
    def test_upload_pdf_415(self) -> None:
        """7：service 抛 DocumentUnsupportedExtensionError → 415。"""
        self.fake_upload_service.upload.side_effect = (
            DocumentUnsupportedExtensionError(
                "unsupported file extension: '.pdf'"
            )
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("report.pdf", b"%PDF-1.4", "application/pdf")},
        )

        self.assertEqual(response.status_code, 415)
        body = response.json()
        self.assertEqual(body["type"], "DocumentUnsupportedExtensionError")

    # ------------------------------------------- 8. 通用 Upload 错误 → 400
    def test_upload_invalid_filename_400(self) -> None:
        """8：其他 DocumentUploadError（如非法文件名）→ 400。"""
        self.fake_upload_service.upload.side_effect = DocumentUploadError(
            "invalid filename"
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("../evil.txt", b"x", "text/plain")},
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["type"], "DocumentUploadError")

    # ------------------------------------------- 8. Parser 错误 → 500
    def test_upload_parser_error_500(self) -> None:
        """8：service 抛 DocumentParserError → 500。"""
        self.fake_upload_service.upload.side_effect = DocumentParserError(
            "read failed"
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["type"], "DocumentParserError")

    def test_upload_chunking_error_500(self) -> None:
        """8：service 抛 DocumentChunkingError → 500。"""
        self.fake_upload_service.upload.side_effect = DocumentChunkingError(
            "split failed"
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )

        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["type"], "DocumentChunkingError")

    # ------------------------------------------- 9. 502 / 503 映射
    def test_upload_embedding_error_502(self) -> None:
        """9：EmbeddingClientError → 502（沿用既有映射）。"""
        self.fake_upload_service.upload.side_effect = EmbeddingClientError(
            "embedding api down"
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["type"], "EmbeddingClientError")

    def test_upload_milvus_error_503(self) -> None:
        """9：MilvusRepositoryError → 503（沿用既有映射）。"""
        self.fake_upload_service.upload.side_effect = MilvusRepositoryError(
            "milvus down"
        )

        response = self.client.post(
            "/documents/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["type"], "MilvusRepositoryError")

    # ------------------------------------------- 10. response schema 校验
    def test_upload_response_schema_exact(self) -> None:
        """10：响应字段恰好 7 个（extra=forbid，无 embedding/chunk 全量）。"""
        self.fake_upload_service.upload.return_value = self._make_document()

        response = self.client.post(
            "/documents/upload",
            files={"file": ("test.txt", b"hello world", "text/plain")},
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(
            set(body.keys()),
            {
                "id",
                "filename",
                "file_size",
                "mime_type",
                "status",
                "chunk_count",
                "error_message",
            },
        )


if __name__ == "__main__":
    unittest.main()
