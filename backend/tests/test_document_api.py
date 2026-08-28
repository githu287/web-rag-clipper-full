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
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_plugin
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
    get_plugin_service,
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
        # Phase 3.5 Step 2-E：业务端点需插件认证 + 插件工作空间 API Key 注入
        self.fake_plugin = SimpleNamespace(plugin_id="plugin-42")
        self.fake_plugin_service = Mock()
        self.fake_plugin_service.decrypt_api_key = Mock(
            return_value="sk-plugin"
        )
        self.app.dependency_overrides[get_current_plugin] = (
            lambda: self.fake_plugin
        )
        self.app.dependency_overrides[get_plugin_service] = (
            lambda: self.fake_plugin_service
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
            plugin_id="plugin-42",  # Phase 3.5 Step 2-E：归属 = 当前插件工作空间
        )

    def test_create_document_with_plugin_id_rejected_422(self) -> None:
        """客户端传 plugin_id → 422（extra=forbid；归属由插件认证决定，不可客户端指定）。"""
        response = self.client.post(
            "/documents",
            json={
                "filename": "a.txt",
                "file_path": "p/a.txt",
                "plugin_id": "other-plugin",
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
            plugin_id="plugin-42",  # Phase 3.5 Step 2-E：归属 + 插件 Key 注入
            api_key="sk-plugin",
        )
        # Phase 3.5 Step 2-E：终态读取带 plugin_id（二次 ownership 校验）
        self.fake_document_repo.get_document.assert_called_once_with(
            1, "plugin-42"
        )

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
        # Phase 3.5 Step 2-E：ownership 约束——delete_document(document_id, plugin_id)
        self.fake_delete_service.delete_document.assert_awaited_once_with(
            1, "plugin-42"
        )

    def test_delete_document_not_found_idempotent_204(self) -> None:
        """DELETE 不存在 document：仍返回 204（幂等，目标态已达成）。"""
        self.fake_delete_service.delete_document.return_value = None

        response = self.client.delete("/documents/999")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        # Phase 3.5 Step 2-E：ownership 约束——delete_document(document_id, plugin_id)
        self.fake_delete_service.delete_document.assert_awaited_once_with(
            999, "plugin-42"
        )

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


class DocumentLibraryApiTest(unittest.TestCase):
    """Phase 3.6 Step 2-D：「我的知识库」GET /documents / GET /documents/{id} API 测试。

    TestClient + dependency_overrides（get_current_plugin / get_document_repository /
    get_plugin_service），不连接真实数据库 / Milvus。
    """

    def setUp(self) -> None:
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_document_repo = Mock()
        self.fake_plugin = SimpleNamespace(plugin_id="plugin-42")
        self.fake_plugin_service = Mock()
        self.fake_plugin_service.decrypt_api_key = Mock(return_value="sk-plugin")

        self.app = create_app()
        self.app.dependency_overrides[get_document_repository] = (
            lambda: self.fake_document_repo
        )
        self.app.dependency_overrides[get_current_plugin] = (
            lambda: self.fake_plugin
        )
        self.app.dependency_overrides[get_plugin_service] = (
            lambda: self.fake_plugin_service
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    def _make_document(
        self,
        doc_id: int = 1,
        title: str | None = "A1",
        filename: str = "webclip.txt",
        url: str | None = "https://a/1",
        source_type: str = "webpage",
        status: str = "SUCCESS",
        chunk_count: int = 3,
        file_size: int = 0,
        error_message: str | None = None,
        mime_type: str = "text/plain",
        created_at: datetime | None = None,
    ) -> Mock:
        """构造带业务字段的 Mock Document（模拟 ORM 可读字段）。"""
        doc = Mock()
        doc.id = doc_id
        doc.title = title
        doc.filename = filename
        doc.url = url
        doc.source_type = source_type
        doc.status = status
        doc.chunk_count = chunk_count
        doc.file_size = file_size
        doc.error_message = error_message
        doc.mime_type = mime_type
        ts = created_at or datetime(2026, 8, 27, 10, 0, 0)
        doc.created_at = ts
        doc.updated_at = ts
        return doc

    def test_list_documents_success_whitelist(self) -> None:
        """GET /documents：200，分页结构正确，列表项为安全白名单字段。"""
        now = datetime(2026, 8, 27, 10, 0, 0)
        doc = self._make_document(doc_id=1, title="A1", created_at=now)
        self.fake_document_repo.count_documents.return_value = 1
        self.fake_document_repo.list_documents.return_value = [doc]

        response = self.client.get("/documents?page=1&page_size=20")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 20)
        self.assertEqual(body["pages"], 1)
        self.assertEqual(len(body["items"]), 1)

        item = body["items"][0]
        self.assertEqual(item["id"], 1)
        self.assertEqual(item["title"], "A1")
        self.assertEqual(item["filename"], "webclip.txt")
        self.assertEqual(item["url"], "https://a/1")
        self.assertEqual(item["source_type"], "webpage")
        self.assertEqual(item["status"], "SUCCESS")
        self.assertEqual(item["chunk_count"], 3)
        self.assertEqual(item["file_size"], 0)
        self.assertIsNone(item["error_message"])
        self.assertEqual(item["created_at"], now.isoformat())
        # 白名单：不包含 plugin_id / file_path / mime_type / updated_at / 安全字段
        for internal in (
            "plugin_id",
            "file_path",
            "mime_type",
            "updated_at",
            "plugin_secret",
            "plugin_secret_hash",
            "api_key_ciphertext",
            "api_key_nonce",
            "APP_MASTER_KEY",
        ):
            self.assertNotIn(internal, item)
        # ownership：plugin_id 来自认证上下文（get_current_plugin）
        self.fake_document_repo.count_documents.assert_called_once_with(
            "plugin-42", keyword=None, status=None, source_type=None
        )
        self.fake_document_repo.list_documents.assert_called_once_with(
            "plugin-42",
            page=1,
            page_size=20,
            keyword=None,
            status=None,
            source_type=None,
        )

    def test_list_documents_plugin_isolation(self) -> None:
        """GET /documents：A / B 视角分别把 plugin_id 传给 repo（身份来自认证上下文）。"""
        self.fake_document_repo.count_documents.return_value = 0
        self.fake_document_repo.list_documents.return_value = []

        self.client.get("/documents")
        self.fake_document_repo.count_documents.assert_called_once_with(
            "plugin-42", keyword=None, status=None, source_type=None
        )

        self.fake_document_repo.reset_mock()
        self.fake_plugin.plugin_id = "plugin-7"
        self.client.get("/documents")
        self.fake_document_repo.count_documents.assert_called_once_with(
            "plugin-7", keyword=None, status=None, source_type=None
        )

    def test_list_documents_query_params_passed(self) -> None:
        """GET /documents：keyword / status / source_type / page / page_size 透传 repo。"""
        self.fake_document_repo.count_documents.return_value = 0
        self.fake_document_repo.list_documents.return_value = []

        response = self.client.get(
            "/documents?page=2&page_size=10&keyword=hello"
            "&status=SUCCESS&source_type=webpage"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pages"], 0)
        self.fake_document_repo.count_documents.assert_called_once_with(
            "plugin-42",
            keyword="hello",
            status="SUCCESS",
            source_type="webpage",
        )
        self.fake_document_repo.list_documents.assert_called_once_with(
            "plugin-42",
            page=2,
            page_size=10,
            keyword="hello",
            status="SUCCESS",
            source_type="webpage",
        )

    def test_list_documents_pagination_validation(self) -> None:
        """GET /documents：page=0 / page_size=0 / page_size=101 / status 非法 / source_type 非法 → 422。"""
        for query in (
            "page=0",
            "page_size=0",
            "page_size=101",
            "status=DELETING",
            "source_type=pdf",
        ):
            with self.subTest(query=query):
                response = self.client.get("/documents?" + query)
                self.assertEqual(response.status_code, 422, msg=query)

    def test_list_documents_extra_query_ignored(self) -> None:
        """GET /documents：未声明 query 参数按现有约束忽略（不 422、不传入 repo）。"""
        self.fake_document_repo.count_documents.return_value = 0
        self.fake_document_repo.list_documents.return_value = []

        response = self.client.get("/documents?extra=1&page=2")

        self.assertEqual(response.status_code, 200)
        self.fake_document_repo.list_documents.assert_called_once_with(
            "plugin-42",
            page=2,
            page_size=20,
            keyword=None,
            status=None,
            source_type=None,
        )

    def test_list_documents_repository_error_503(self) -> None:
        """GET /documents：DocumentOperationError → 503。"""
        self.fake_document_repo.count_documents.side_effect = (
            DocumentOperationError("mysql connection lost")
        )

        response = self.client.get("/documents")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["type"], "DocumentOperationError")

    def test_get_document_detail_success_whitelist(self) -> None:
        """GET /documents/{id}：200，详情字段完整，白名单不含内部字段。"""
        now = datetime(2026, 8, 27, 10, 0, 0)
        doc = self._make_document(
            doc_id=5, title="A5", status="SUCCESS", chunk_count=4, created_at=now
        )
        self.fake_document_repo.get_document.return_value = doc

        response = self.client.get("/documents/5")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], 5)
        self.assertEqual(body["title"], "A5")
        self.assertEqual(body["mime_type"], "text/plain")
        self.assertEqual(body["updated_at"], now.isoformat())
        self.assertNotIn("plugin_id", body)
        self.assertNotIn("file_path", body)
        for internal in (
            "plugin_secret",
            "plugin_secret_hash",
            "api_key_ciphertext",
            "api_key_nonce",
            "APP_MASTER_KEY",
        ):
            self.assertNotIn(internal, body)
        self.fake_document_repo.get_document.assert_called_once_with(5, "plugin-42")

    def test_get_document_detail_cross_plugin_404(self) -> None:
        """GET /documents/{id}：跨 Plugin → 404，不泄露归属。"""
        self.fake_document_repo.get_document.side_effect = DocumentNotFoundError(
            "document not found: id=99"
        )

        response = self.client.get("/documents/99")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["type"], "DocumentNotFoundError")
        self.assertNotIn("plugin", response.text)

    def test_get_document_detail_repository_error_503(self) -> None:
        """GET /documents/{id}：DocumentOperationError → 503。"""
        self.fake_document_repo.get_document.side_effect = DocumentOperationError(
            "mysql connection lost"
        )

        response = self.client.get("/documents/5")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["type"], "DocumentOperationError")


class DocumentLibraryApiUnauthenticatedTest(unittest.TestCase):
    """Phase 3.6 Step 2-D：无插件凭证访问知识库 API → 401（不调用 repo）。"""

    def setUp(self) -> None:
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_document_repo = Mock()
        self.app = create_app()
        self.app.dependency_overrides[get_document_repository] = (
            lambda: self.fake_document_repo
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    def test_list_unauthenticated_401(self) -> None:
        """GET /documents：无凭证 → 401，repo 不被调用。"""
        response = self.client.get("/documents")
        self.assertEqual(response.status_code, 401)
        self.fake_document_repo.count_documents.assert_not_called()
        self.fake_document_repo.list_documents.assert_not_called()

    def test_detail_unauthenticated_401(self) -> None:
        """GET /documents/{id}：无凭证 → 401，repo 不被调用。"""
        response = self.client.get("/documents/1")
        self.assertEqual(response.status_code, 401)
        self.fake_document_repo.get_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
