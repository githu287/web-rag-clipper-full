"""
POST /rag/search API 集成测试（Phase 2.13 Step 2；Phase 3.1 Step 3 扩展）。

技术栈：unittest + unittest.mock + FastAPI TestClient。
不连接真实 MySQL / Milvus / 百炼：
    - 通过 app.dependency_overrides 将 get_rag_service 替换为 fake rag service
      （AsyncMock(search)），聚焦「路由 + HTTP 映射 + response schema」；
    - 通过 patch("backend.main.get_milvus_initializer") 阻断 lifespan 启动期
      真实 Milvus 连接。

覆盖场景（对应 Phase 2.13 Step 2 §九 / Phase 2.13 Step 3 §四 API 测试要求）：
    A. HTTP 200 成功，原有 5 字段全部存在
    B. 新增 metadata 4 字段存在（document_id / filename / status / created_at）
    C. document_id == page_id（1:1 关联）
    D. status == SUCCESS
    E. JSON 类型正确（created_at 可序列化为 ISO 8601 字符串）
    F. response schema 字段恰好 12 个（Phase 3.1 Step 3 新增 title/url/source_type）
    G. query 为空 → 422
    H. limit=0 → 422
    I. limit=21 → 422
    J. extra request 字段 → 422
    K. 空结果 → 200 + results=[]
    L. DocumentRepository 异常由 main.py handler 转换为 503（不在 Router 内吞掉）

Phase 3.1 Step 3 新增覆盖：
    3b. webpage 剪藏 title/url/source_type 正确序列化返回
    3c. 上传文档 title/url=None、source_type=upload 序列化正确
"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_user
from backend.core.di import get_rag_service, get_user_service
from backend.core.exceptions import DocumentOperationError
from backend.main import create_app
from backend.models.api_schema import RagSearchResult


class RagApiTest(unittest.TestCase):
    """POST /rag/search API 集成测试。"""

    def setUp(self) -> None:
        """构造隔离 app + fake rag service + 阻断 lifespan Milvus 连接。"""
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_rag_service = Mock()
        self.fake_rag_service.search = AsyncMock()

        self.app = create_app()
        self.app.dependency_overrides[get_rag_service] = (
            lambda: self.fake_rag_service
        )
        # Phase 3.4 Step 4：业务端点需认证 + 用户 API Key 注入
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

    def _make_result(
        self,
        pk: str = "1_0",
        page_id: int = 1,
        chunk_index: int = 0,
        chunk_text: str = "rag clipper marker content",
        distance: float = 0.82,
        document_id: int = 1,
        filename: str = "notes.txt",
        status: str = "SUCCESS",
        created_at: datetime | None = datetime(2026, 8, 23, 10, 0, 0),
        title: str | None = None,
        url: str | None = None,
        source_type: str | None = None,
    ) -> RagSearchResult:
        return RagSearchResult(
            id=pk,
            page_id=page_id,
            chunk_index=chunk_index,
            chunk_text=chunk_text,
            distance=distance,
            document_id=document_id,
            filename=filename,
            status=status,
            created_at=created_at,
            title=title,
            url=url,
            source_type=source_type,
        )

    # ----------------------------------------------------- 1. HTTP 200 + 原有字段
    def test_search_200_original_fields_present(self) -> None:
        """1：HTTP 200，原有 5 字段全部存在。"""
        self.fake_rag_service.search.return_value = [self._make_result()]

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        self.assertEqual(response.status_code, 200)
        item = response.json()["results"][0]
        for field in ("id", "page_id", "chunk_index", "chunk_text", "distance"):
            self.assertIn(field, item)
        self.assertEqual(item["id"], "1_0")
        self.assertEqual(item["page_id"], 1)
        self.assertEqual(item["chunk_index"], 0)
        self.assertEqual(item["chunk_text"], "rag clipper marker content")
        self.assertEqual(item["distance"], 0.82)

    # ----------------------------------------------------- 2. metadata 字段存在
    def test_search_metadata_fields_present(self) -> None:
        """2：新增 metadata 4 字段存在且值正确。"""
        self.fake_rag_service.search.return_value = [self._make_result()]

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        self.assertEqual(response.status_code, 200)
        item = response.json()["results"][0]
        for field in ("document_id", "filename", "status", "created_at"):
            self.assertIn(field, item)
        self.assertEqual(item["document_id"], 1)
        self.assertEqual(item["filename"], "notes.txt")
        self.assertEqual(item["status"], "SUCCESS")
        self.assertIsNotNone(item["created_at"])

    # --------------------------------------------------------- 3. JSON 类型正确
    def test_search_metadata_json_types(self) -> None:
        """3：JSON 类型正确（int / str / ISO 字符串）。"""
        self.fake_rag_service.search.return_value = [self._make_result()]

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        item = response.json()["results"][0]
        self.assertIsInstance(item["id"], str)
        self.assertIsInstance(item["page_id"], int)
        self.assertIsInstance(item["chunk_index"], int)
        self.assertIsInstance(item["chunk_text"], str)
        self.assertIsInstance(item["distance"], float)
        self.assertIsInstance(item["document_id"], int)
        self.assertIsInstance(item["filename"], str)
        self.assertIsInstance(item["status"], str)
        self.assertIsInstance(item["created_at"], str)  # ISO 8601 序列化

    # ------------------------------------ 3b. Phase 3.1 Step 3：网页来源元数据返回
    def test_search_webclip_metadata_returned(self) -> None:
        """3b：title/url/source_type 正确序列化返回（webpage 剪藏）。"""
        self.fake_rag_service.search.return_value = [
            self._make_result(
                title="示例文章",
                url="https://example.com/article/1",
                source_type="webpage",
            )
        ]

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        self.assertEqual(response.status_code, 200)
        item = response.json()["results"][0]
        self.assertEqual(item["title"], "示例文章")
        self.assertEqual(item["url"], "https://example.com/article/1")
        self.assertEqual(item["source_type"], "webpage")

    def test_search_upload_metadata_defaults_none(self) -> None:
        """3c：上传文档 title/url=None、source_type=upload 序列化正确。"""
        self.fake_rag_service.search.return_value = [
            self._make_result(source_type="upload")
        ]

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        self.assertEqual(response.status_code, 200)
        item = response.json()["results"][0]
        self.assertIsNone(item["title"])
        self.assertIsNone(item["url"])
        self.assertEqual(item["source_type"], "upload")

    # ------------------------------------------------- 4. document_id == page_id
    def test_search_document_id_equals_page_id(self) -> None:
        """4：document_id == page_id（1:1 关联）。"""
        self.fake_rag_service.search.return_value = [
            self._make_result(page_id=11, document_id=11),
        ]

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        item = response.json()["results"][0]
        self.assertEqual(item["document_id"], 11)
        self.assertEqual(item["page_id"], 11)
        self.assertEqual(item["document_id"], item["page_id"])

    # ------------------------------------------- 5. limit / query 校验不变
    def test_search_empty_query_rejected(self) -> None:
        """5a：query 为空 → 422（不触发 service）。"""
        response = self.client.post("/rag/search", json={"query": ""})

        self.assertEqual(response.status_code, 422)
        self.fake_rag_service.search.assert_not_called()

    def test_search_limit_zero_rejected(self) -> None:
        """5b：limit=0 越界 → 422。"""
        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 0},
        )

        self.assertEqual(response.status_code, 422)
        self.fake_rag_service.search.assert_not_called()

    def test_search_limit_over_20_rejected(self) -> None:
        """5c：limit=21 越界 → 422。"""
        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 21},
        )

        self.assertEqual(response.status_code, 422)
        self.fake_rag_service.search.assert_not_called()

    def test_search_extra_field_rejected(self) -> None:
        """5d：请求带未知字段 → 422（extra=forbid）。"""
        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5, "extra": 1},
        )

        self.assertEqual(response.status_code, 422)
        self.fake_rag_service.search.assert_not_called()

    def test_search_query_and_limit_passthrough(self) -> None:
        """5e：query / limit 正确透传给 service。"""
        self.fake_rag_service.search.return_value = [self._make_result()]

        response = self.client.post(
            "/rag/search",
            json={"query": "hello world", "limit": 20},
        )

        self.assertEqual(response.status_code, 200)
        # Phase 3.4 Step 4：user_id（ownership）+ api_key（用户自己的 Key）透传
        self.fake_rag_service.search.assert_awaited_once_with(
            query="hello world",
            limit=20,
            user_id=1,
            api_key="sk-test",
        )

    # ----------------------------------------------------- 6. 空结果 → 200 + []
    def test_search_empty_results_200(self) -> None:
        """6：空结果 → HTTP 200 + results=[]。"""
        self.fake_rag_service.search.return_value = []

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": []})

    # ------------------------------------------- 7. response schema 恰好 12 字段
    def test_search_response_schema_exact(self) -> None:
        """7：响应 item 恰好 12 个字段（Phase 3.1 Step 3：新增 title/url/source_type）。"""
        self.fake_rag_service.search.return_value = [self._make_result()]

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        self.assertEqual(response.status_code, 200)
        item = response.json()["results"][0]
        self.assertEqual(
            set(item.keys()),
            {
                "id",
                "page_id",
                "chunk_index",
                "chunk_text",
                "distance",
                "document_id",
                "filename",
                "status",
                "created_at",
                "title",
                "url",
                "source_type",
            },
        )

    # --------------------------------------- L. DocumentRepository 异常 → 503
    def test_search_repository_error_503(self) -> None:
        """L：DocumentRepository 异常由 main.py handler 转 503（Router 不吞）。"""
        self.fake_rag_service.search.side_effect = DocumentOperationError(
            "get_documents_by_ids failed: db down"
        )

        response = self.client.post(
            "/rag/search",
            json={"query": "hello", "limit": 5},
        )

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["type"], "DocumentOperationError")


if __name__ == "__main__":
    unittest.main()
