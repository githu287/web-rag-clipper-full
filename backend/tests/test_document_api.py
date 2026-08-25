"""
Document 生命周期 API 集成测试（Phase 2.9 Step 3；Phase 2.11 Step 2 增 DELETE）。

技术栈：unittest + unittest.mock + FastAPI TestClient。
不连接真实 MySQL / Milvus / 百炼：
    - 通过 app.dependency_overrides 将 get_document_repository /
      get_document_ingest_service / get_document_delete_service 替换为 Mock；
    - 通过 patch("backend.main.get_milvus_initializer") 阻断 lifespan 启动期
      真实 Milvus 连接（lifespan 直接调用该函数，不走 Depends）。

覆盖场景：
    1. POST /documents             → 201，status=PENDING，chunk_count=0
    2. POST /documents/{id}/ingest → 200，SUCCESS + chunk_count=len(chunks)
    3. document_id 不存在           → 404（DocumentNotFoundError）
    4. EmbeddingClientError        → 502
    5. MilvusRepositoryError       → 503
    6. 非法 chunks（空列表）        → 422
    7. DocumentOperationError      → 503（创建失败）
    8. DELETE /documents/{id}      → 204，无 body；不存在也 204（幂等）
    9. DELETE 服务失败（PROCESSING 拒绝 / MySQL 失败）→ 503
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_user
from backend.clients.embedding import EmbeddingClientError
from backend.core.exceptions import (
    DocumentNotFoundError,
    DocumentOperationError,
    MilvusRepositoryError,
)
from backend.core.di import (
    get_document_delete_service,
    get_document_ingest_service,
    get_document_repository,
    get_user_service,
)
from backend.main import create_app


class DocumentApiTest(unittest.TestCase):
    """Document 生命周期 API 集成测试（TestClient + dependency_overrides）。"""

    def setUp(self) -> None:
        """构造隔离的 FastAPI app + Mock 依赖 + 阻断 lifespan Milvus 连接。"""
        # 阻断 lifespan：lifespan 直接调用 get_milvus_initializer()（不走 Depends）
        self._milvus_init_patcher = patch(
            "backend.main.get_milvus_initializer"
        )
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        # Mock 依赖
        self.fake_document_repo = Mock()
        self.fake_ingest_service = Mock()
        self.fake_ingest_service.ingest_document = AsyncMock(
            return_value=None
        )
        self.fake_delete_service = Mock()
        self.fake_delete_service.delete_document = AsyncMock(
            return_value=None
        )

        # 构造 app 并 override 依赖
        self.app = create_app()
        self.app.dependency_overrides[get_document_repository] = (
            lambda: self.fake_document_repo
        )
        self.app.dependency_overrides[get_document_ingest_service] = (
            lambda: self.fake_ingest_service
        )
        self.app.dependency_overrides[get_document_delete_service] = (
            lambda: self.fake_delete_service
        )
        # Phase 3.4 Step 4：业务端点需认证（Bearer token → current_user）+ 用户 API Key
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
        """停止 lifespan 阻断 patch。"""
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    def _make_document(
        self,
        doc_id: int = 1,
        filename: str = "test.txt",
        file_path: str = "data/test.txt",
        status: str = "PENDING",
        chunk_count: int = 0,
    ) -> Mock:
        """构造带属性的 Mock Document（模拟 ORM 对象可读字段）。"""
        doc = Mock()
        doc.id = doc_id
        doc.filename = filename
        doc.file_path = file_path
        doc.status = status
        doc.chunk_count = chunk_count
        return doc

    # --------------------------------------------------- 1. POST /documents 创建
    def test_create_document_success(self) -> None:
        """创建 Document：201，status=PENDING，chunk_count=0。"""
        created = self._make_document(
            doc_id=1,
            filename="test.txt",
            file_path="data/test.txt",
            status="PENDING",
            chunk_count=0,
        )
        self.fake_document_repo.create_document.return_value = created

        response = self.client.post(
            "/documents",
            json={
                "filename": "test.txt",
                "file_path": "data/test.txt",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "id": 1,
                "filename": "test.txt",
                "file_path": "data/test.txt",
                "status": "PENDING",
                "chunk_count": 0,
            },
        )
        self.fake_document_repo.create_document.assert_called_once_with(
            filename="test.txt",
            file_path="data/test.txt",
            user_id=1,  # Phase 3.4 Step 4：归属 = current_user.id（Bearer token）
        )

    def test_create_document_with_user_id_rejected_422(self) -> None:
        """客户端传 user_id → 422（extra=forbid；Phase 3.4 Step 4 归属由 token 决定）。"""
        response = self.client.post(
            "/documents",
            json={
                "filename": "a.txt",
                "file_path": "p/a.txt",
                "user_id": 42,
            },
        )

        self.assertEqual(response.status_code, 422)
        self.fake_document_repo.create_document.assert_not_called()

    # ------------------------------------------- 2. POST /documents/{id}/ingest
    def test_ingest_document_success(self) -> None:
        """生命周期 ingest 成功：200，SUCCESS + chunk_count=len(chunks)。"""
        chunks = ["chunk A", "chunk B", "chunk C"]
        # ingest 成功后终态读取：SUCCESS / chunk_count=3
        self.fake_document_repo.get_document.return_value = self._make_document(
            doc_id=1,
            status="SUCCESS",
            chunk_count=3,
        )

        response = self.client.post(
            "/documents/1/ingest",
            json={"chunks": chunks},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "document_id": 1,
                "status": "SUCCESS",
                "chunk_count": 3,
            },
        )
        self.fake_ingest_service.ingest_document.assert_awaited_once_with(
            document_id=1,
            chunks=chunks,
            user_id=1,  # Phase 3.4 Step 4：归属 + 用户 Key 注入
            api_key="sk-test",
        )
        # Phase 3.4 Step 4：终态读取带 user_id（二次 ownership 校验）
        self.fake_document_repo.get_document.assert_called_once_with(1, 1)

    # ------------------------------------------- 3. document_id 不存在 → 404
    def test_ingest_document_not_found(self) -> None:
        """document_id 不存在：ingest_document 抛 DocumentNotFoundError → 404。"""
        not_found = DocumentNotFoundError("document not found: id=999")
        self.fake_ingest_service.ingest_document.side_effect = not_found

        response = self.client.post(
            "/documents/999/ingest",
            json={"chunks": ["x"]},
        )

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["detail"], f"Document 不存在：{not_found}")
        self.assertEqual(body["type"], "DocumentNotFoundError")
        # 异常路径：不执行终态读取
        self.fake_document_repo.get_document.assert_not_called()

    # ------------------------------------------- 4. EmbeddingClientError → 502
    def test_ingest_embedding_error(self) -> None:
        """EmbeddingClientError：502 Bad Gateway。"""
        self.fake_ingest_service.ingest_document.side_effect = (
            EmbeddingClientError("embedding api down")
        )

        response = self.client.post(
            "/documents/1/ingest",
            json={"chunks": ["x"]},
        )

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["type"], "EmbeddingClientError")

    # ------------------------------------------- 5. MilvusRepositoryError → 503
    def test_ingest_milvus_error(self) -> None:
        """MilvusRepositoryError：503 Service Unavailable。"""
        self.fake_ingest_service.ingest_document.side_effect = (
            MilvusRepositoryError("milvus down")
        )

        response = self.client.post(
            "/documents/1/ingest",
            json={"chunks": ["x"]},
        )

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["type"], "MilvusRepositoryError")

    # --------------------------------------------------- 6. 非法 chunks → 422
    def test_ingest_empty_chunks_rejected(self) -> None:
        """非法 chunks（空列表）：Pydantic 校验 → 422。"""
        response = self.client.post(
            "/documents/1/ingest",
            json={"chunks": []},
        )

        self.assertEqual(response.status_code, 422)
        # 校验失败不触发 service 调用
        self.fake_ingest_service.ingest_document.assert_not_called()

    def test_create_document_missing_field_rejected(self) -> None:
        """非法创建请求（缺 filename）：Pydantic 校验 → 422。"""
        response = self.client.post(
            "/documents",
            json={"file_path": "data/test.txt"},
        )

        self.assertEqual(response.status_code, 422)
        self.fake_document_repo.create_document.assert_not_called()

    # ------------------------------------------- 7. DocumentOperationError → 503
    def test_create_document_operation_error(self) -> None:
        """创建 Document 数据库失败：DocumentOperationError → 503。"""
        self.fake_document_repo.create_document.side_effect = (
            DocumentOperationError("mysql connection lost")
        )

        response = self.client.post(
            "/documents",
            json={
                "filename": "test.txt",
                "file_path": "data/test.txt",
            },
        )

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["type"], "DocumentOperationError")

    # ----------------------------------------- 8. DELETE /documents/{id}（Phase 2.11 Step 2）
    def test_delete_document_success_204(self) -> None:
        """DELETE 成功：204 No Content，无 body；service.delete_document 被调用。"""
        response = self.client.delete("/documents/1")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        # Phase 3.4 Step 4：ownership 约束——delete_document(document_id, user_id)
        self.fake_delete_service.delete_document.assert_awaited_once_with(1, 1)

    def test_delete_document_not_found_idempotent_204(self) -> None:
        """DELETE 不存在 document：仍返回 204（幂等，目标态已达成）。"""
        self.fake_delete_service.delete_document.return_value = None

        response = self.client.delete("/documents/999")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        # Phase 3.4 Step 4：ownership 约束——delete_document(document_id, user_id)
        self.fake_delete_service.delete_document.assert_awaited_once_with(999, 1)

    # ----------------------------------------- 9. DELETE 服务失败（Phase 2.11 Step 2）
    def test_delete_document_processing_conflict_503(self) -> None:
        """DELETE 遇 PROCESSING（ingest 进行中）：DocumentOperationError → 503。"""
        self.fake_delete_service.delete_document.side_effect = (
            DocumentOperationError(
                "document is being ingested, delete rejected: id=1"
            )
        )

        response = self.client.delete("/documents/1")

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["type"], "DocumentOperationError")

    def test_delete_document_mysql_failure_503(self) -> None:
        """DELETE 时 MySQL 删除失败：保持 DELETING，DocumentOperationError → 503。"""
        self.fake_delete_service.delete_document.side_effect = (
            DocumentOperationError("mysql delete failed: id=1")
        )

        response = self.client.delete("/documents/1")

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["type"], "DocumentOperationError")


if __name__ == "__main__":
    unittest.main()
