"""
POST /ingest/page API 集成测试（Phase 2.13 Step 3）。

技术栈：unittest + unittest.mock + FastAPI TestClient。
不连接真实 MySQL / Milvus / 百炼：
    - 通过 app.dependency_overrides 将 get_ingest_service 替换为 fake ingest
      service（AsyncMock(ingest_page)），聚焦「路由 + HTTP 映射 + response schema」；
    - 通过 patch("backend.main.get_milvus_initializer") 阻断 lifespan 启动期
      真实 Milvus 连接。

覆盖场景（对应 Phase 2.13 Step 3 §五 A~K）：
    A. 正常请求（page_id=123, chunks=["chunk A","chunk B"]）→ 200，参数透传
    B. response：success=true，message="success"
    C. page_id <= 0（0 / 负数）→ 422
    D. chunks=[] → 422
    E. chunks 不是 list → 422
    F. chunks 包含非字符串元素 → 422
    G. EmbeddingClientError → 502
    H. MilvusRepositoryError → 503
    I. Service 异常向上传播（Router 不吞；未知异常 → 500，已知异常 → 502/503）
    J. dependency_overrides 正确注入 mock service（fake 被调用）
    K. Router 不直接构造 IngestService（依赖 Depends(get_ingest_service)；
       源码审查：ingest_page 签名 service: IngestService = Depends(get_ingest_service)，
       路由体无 IngestService(...) 构造；本测试验证 Depends 注入链路生效）
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.clients.embedding import EmbeddingClientError
from backend.core.di import get_ingest_service
from backend.core.exceptions import MilvusRepositoryError
from backend.main import create_app


class IngestApiTest(unittest.TestCase):
    """POST /ingest/page API 集成测试（TestClient + dependency_overrides）。"""

    def setUp(self) -> None:
        """构造隔离的 FastAPI app + fake ingest service + 阻断 lifespan Milvus。"""
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_ingest_service = Mock()
        self.fake_ingest_service.ingest_page = AsyncMock(return_value=None)

        self.app = create_app()
        self.app.dependency_overrides[get_ingest_service] = (
            lambda: self.fake_ingest_service
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    def _post(self, payload: dict):
        return self.client.post("/ingest/page", json=payload)

    # ----------------------------------------------------- A. 正常请求 → 200
    def test_ingest_page_success(self) -> None:
        """A：正常请求 → 200，page_id / chunks 正确透传。"""
        response = self._post(
            {"page_id": 123, "chunks": ["chunk A", "chunk B"]}
        )

        self.assertEqual(response.status_code, 200)
        self.fake_ingest_service.ingest_page.assert_awaited_once_with(
            page_id=123,
            chunks=["chunk A", "chunk B"],
        )

    # ------------------------------------------- B. response success=true
    def test_ingest_page_response_body(self) -> None:
        """B：response 为 success=true + message="success"。"""
        response = self._post(
            {"page_id": 123, "chunks": ["chunk A"]}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"success": True, "message": "success"},
        )

    # ------------------------------------------- C. page_id <= 0 → 422
    def test_ingest_page_id_zero_rejected(self) -> None:
        """C：page_id=0 → 422（gt=0），不触发 service。"""
        response = self._post({"page_id": 0, "chunks": ["chunk A"]})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_page.assert_not_called()

    def test_ingest_page_id_negative_rejected(self) -> None:
        """C：page_id=-1 → 422，不触发 service。"""
        response = self._post({"page_id": -1, "chunks": ["chunk A"]})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_page.assert_not_called()

    # ----------------------------------------------- D. chunks=[] → 422
    def test_ingest_empty_chunks_rejected(self) -> None:
        """D：chunks=[] → 422（min_length=1），不触发 service。"""
        response = self._post({"page_id": 123, "chunks": []})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_page.assert_not_called()

    # ------------------------------------------- E. chunks 不是 list → 422
    def test_ingest_chunks_not_list_rejected(self) -> None:
        """E：chunks 为字符串 → 422，不触发 service。"""
        response = self._post({"page_id": 123, "chunks": "chunk A"})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_page.assert_not_called()

    # ------------------------------- F. chunks 包含非字符串元素 → 422
    def test_ingest_chunks_with_non_string_rejected(self) -> None:
        """F：chunks 含 int 元素 → 422，不触发 service。"""
        response = self._post({"page_id": 123, "chunks": ["chunk A", 1]})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_page.assert_not_called()

    # --------------------------------------- G. EmbeddingClientError → 502
    def test_ingest_embedding_error_502(self) -> None:
        """G：EmbeddingClientError → 502 Bad Gateway（全局 handler 转换）。"""
        self.fake_ingest_service.ingest_page.side_effect = EmbeddingClientError(
            "embedding api down"
        )

        response = self._post({"page_id": 123, "chunks": ["chunk A"]})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["type"], "EmbeddingClientError")

    # ------------------------------------- H. MilvusRepositoryError → 503
    def test_ingest_milvus_error_503(self) -> None:
        """H：MilvusRepositoryError → 503 Service Unavailable（全局 handler 转换）。"""
        self.fake_ingest_service.ingest_page.side_effect = MilvusRepositoryError(
            "milvus upsert failed"
        )

        response = self._post({"page_id": 123, "chunks": ["chunk A"]})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["type"], "MilvusRepositoryError")

    # --------------------- I. Service 异常向上传播（Router 不在路由内吞异常）
    def test_ingest_unexpected_error_propagates(self) -> None:
        """I：未知 Service 异常向上传播 → FastAPI 默认 500（未被 Router 吞为 200）。"""
        self.fake_ingest_service.ingest_page.side_effect = RuntimeError(
            "unexpected boom"
        )
        # raise_server_exceptions=False：500 时返回响应而非重抛服务器异常
        client = TestClient(self.app, raise_server_exceptions=False)

        response = client.post(
            "/ingest/page",
            json={"page_id": 123, "chunks": ["chunk A"]},
        )

        self.assertEqual(response.status_code, 500)

    # --------------------- J. dependency_overrides 正确注入 mock service
    def test_ingest_dependency_override_effective(self) -> None:
        """J：override 后路由调用的是 fake service（fake 被 await 调用）。"""
        response = self._post({"page_id": 7, "chunks": ["x"]})

        self.assertEqual(response.status_code, 200)
        self.fake_ingest_service.ingest_page.assert_awaited_once()
        # 确认 fake 收到的是实际请求参数（而非默认值）
        self.fake_ingest_service.ingest_page.assert_awaited_once_with(
            page_id=7,
            chunks=["x"],
        )

    # ---------------------------------- K. Router 不直接构造 IngestService
    def test_ingest_router_depends_injection(self) -> None:
        """
        K：路由通过 Depends 注入 service（不直接构造 IngestService）。

        - 源码审查（backend/api/routers/ingest.py）：路由签名
          `service: IngestService = Depends(get_ingest_service)`，路由体内无
          `IngestService(...)` 构造（已人工确认）；
        - 本测试验证：endpoint 存在（OpenAPI）+ Depends 链路可替换
          （override 后调用 fake 而非真实 DI 工厂）。
        """
        # endpoint 存在且为 POST（OpenAPI 契约层验证）
        spec = self.app.openapi()
        self.assertIn("/ingest/page", spec["paths"])
        self.assertIn("post", spec["paths"]["/ingest/page"])

        # override 生效（调用 fake 而非真实 DI 工厂）→ Depends 链路可替换
        self.fake_ingest_service.ingest_page.side_effect = MilvusRepositoryError(
            "injected fake path"
        )
        response = self._post({"page_id": 3, "chunks": ["y"]})
        self.assertEqual(response.status_code, 503)
        self.fake_ingest_service.ingest_page.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
