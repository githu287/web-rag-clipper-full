"""Regression tests for document ownership and lifecycle contracts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_plugin
from backend.clients.embedding import EmbeddingClientError
from backend.core.di import get_document_ingest_service, get_plugin_service
from backend.core.exceptions import ApiKeyNotConfiguredError, MilvusRepositoryError
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
        self.fake_ingest_service.ingest_document = AsyncMock(return_value=None)

        # Phase 3.5 Step 2-E：认证 + 插件工作空间 Key 注入
        self.fake_plugin = SimpleNamespace(plugin_id="plugin-42")
        self.fake_plugin_service = Mock()
        self.fake_plugin_service.decrypt_api_key = Mock(return_value="sk-plugin")

        self.app = create_app()
        self.app.dependency_overrides[get_document_ingest_service] = (
            lambda: self.fake_ingest_service
        )
        self.app.dependency_overrides[get_current_plugin] = lambda: self.fake_plugin
        self.app.dependency_overrides[get_plugin_service] = (
            lambda: self.fake_plugin_service
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    def _post(self, payload: dict):
        return self.client.post("/ingest/page", json=payload)

    # ----------------------------------------------------- A. 正常请求 → 200
    def test_ingest_page_success(self) -> None:
        """A：正常请求 → 200，page_id / chunks / plugin_id / api_key 正确透传。"""
        response = self._post(
            {"page_id": 123, "chunks": ["chunk A", "chunk B"]}
        )

        self.assertEqual(response.status_code, 200)
        # Step 2-E：decrypt_api_key(current_plugin) 被调用，返回插件工作空间 Key
        self.fake_plugin_service.decrypt_api_key.assert_called_once_with(
            self.fake_plugin
        )
        self.fake_ingest_service.ingest_document.assert_awaited_once_with(
            document_id=123,
            chunks=["chunk A", "chunk B"],
            plugin_id="plugin-42",
            api_key="sk-plugin",
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
        self.fake_ingest_service.ingest_document.assert_not_called()

    def test_ingest_page_id_negative_rejected(self) -> None:
        """C：page_id=-1 → 422，不触发 service。"""
        response = self._post({"page_id": -1, "chunks": ["chunk A"]})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_document.assert_not_called()

    # ----------------------------------------------- D. chunks=[] → 422
    def test_ingest_empty_chunks_rejected(self) -> None:
        """D：chunks=[] → 422（min_length=1），不触发 service。"""
        response = self._post({"page_id": 123, "chunks": []})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_document.assert_not_called()

    # ------------------------------------------- E. chunks 不是 list → 422
    def test_ingest_chunks_not_list_rejected(self) -> None:
        """E：chunks 为字符串 → 422，不触发 service。"""
        response = self._post({"page_id": 123, "chunks": "chunk A"})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_document.assert_not_called()

    # ------------------------------- F. chunks 包含非字符串元素 → 422
    def test_ingest_chunks_with_non_string_rejected(self) -> None:
        """F：chunks 含 int 元素 → 422，不触发 service。"""
        response = self._post({"page_id": 123, "chunks": ["chunk A", 1]})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_document.assert_not_called()

    # --------------------------------------- G. EmbeddingClientError → 502
    def test_ingest_embedding_error_502(self) -> None:
        """G：EmbeddingClientError → 502 Bad Gateway（全局 handler 转换）。"""
        self.fake_ingest_service.ingest_document.side_effect = EmbeddingClientError(
            "embedding api down"
        )

        response = self._post({"page_id": 123, "chunks": ["chunk A"]})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["type"], "EmbeddingClientError")

    # ------------------------------------- H. MilvusRepositoryError → 503
    def test_ingest_milvus_error_503(self) -> None:
        """H：MilvusRepositoryError → 503 Service Unavailable（全局 handler 转换）。"""
        self.fake_ingest_service.ingest_document.side_effect = MilvusRepositoryError(
            "milvus upsert failed"
        )

        response = self._post({"page_id": 123, "chunks": ["chunk A"]})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["type"], "MilvusRepositoryError")

    # --------------------- I. Service 异常向上传播（Router 不在路由内吞异常）
    def test_ingest_unexpected_error_propagates(self) -> None:
        """I：未知 Service 异常向上传播 → FastAPI 默认 500（未被 Router 吞为 200）。"""
        self.fake_ingest_service.ingest_document.side_effect = RuntimeError(
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
        self.fake_ingest_service.ingest_document.assert_awaited_once()
        # 确认 fake 收到的是实际请求参数（而非默认值）
        self.fake_ingest_service.ingest_document.assert_awaited_once_with(
            document_id=7,
            chunks=["x"],
            plugin_id="plugin-42",
            api_key="sk-plugin",
        )

    # ---------------------------------- K. Router 不直接构造 IngestService
    def test_ingest_router_depends_injection(self) -> None:
        """
        K：路由通过 Depends 注入 service（不直接构造 IngestService）。

        - 源码审查（backend/api/routers/ingest.py）：路由签名
          `service: IngestService = Depends(get_document_ingest_service)`，路由体内无
          `IngestService(...)` 构造（已人工确认）；
        - 本测试验证：endpoint 存在（OpenAPI）+ Depends 链路可替换
          （override 后调用 fake 而非真实 DI 工厂）。
        """
        # endpoint 存在且为 POST（OpenAPI 契约层验证）
        spec = self.app.openapi()
        self.assertIn("/ingest/page", spec["paths"])
        self.assertIn("post", spec["paths"]["/ingest/page"])

        # override 生效（调用 fake 而非真实 DI 工厂）→ Depends 链路可替换
        self.fake_ingest_service.ingest_document.side_effect = MilvusRepositoryError(
            "injected fake path"
        )
        response = self._post({"page_id": 3, "chunks": ["y"]})
        self.assertEqual(response.status_code, 503)
        self.fake_ingest_service.ingest_document.assert_awaited_once()

    # ============================ L. chunks 批量与单条长度上界（防超大请求阻塞与烧费）
    def test_ingest_chunks_over_batch_limit_rejected(self) -> None:
        """L：chunks 1001 项 → 422（与 DocumentIngestRequest 同为 1000 上限），不触发 service。"""
        response = self._post({"page_id": 1, "chunks": ["x"] * 1001})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_document.assert_not_called()

    def test_ingest_chunks_at_batch_limit_accepted(self) -> None:
        """L：恰好 1000 项仍在边界内 → 200。"""
        response = self._post({"page_id": 1, "chunks": ["x"] * 1000})

        self.assertEqual(response.status_code, 200)

    def test_ingest_chunk_over_byte_limit_rejected(self) -> None:
        """L：单条 4098 UTF-8 字节（Milvus chunk_text VARCHAR(4096)）→ 422，不触发 service。"""
        response = self._post({"page_id": 1, "chunks": ["x" * 4096, "汉" * 1366]})

        self.assertEqual(response.status_code, 422)
        self.fake_ingest_service.ingest_document.assert_not_called()

    def test_ingest_chunk_at_byte_limit_accepted(self) -> None:
        """L：单条恰好 4095 UTF-8 字节（多字节字符按字节计）→ 200。"""
        response = self._post({"page_id": 1, "chunks": ["汉" * 1365]})

        self.assertEqual(response.status_code, 200)

    # ================================================================
    # Phase 3.5 Step 2-E：认证 + 插件工作空间 API Key
    # ================================================================
    def test_ingest_page_requires_authentication(self) -> None:
        """F6-1：无凭证（无 X-Plugin-ID / X-Plugin-Secret）→ 401，禁止 anonymous ingest。"""
        # 移除认证 override，走真实 get_current_plugin（无凭证 → 401）
        self.app.dependency_overrides.pop(get_current_plugin, None)
        self.app.dependency_overrides.pop(get_plugin_service, None)

        response = self._post({"page_id": 1, "chunks": ["x"]})

        self.assertEqual(response.status_code, 401)
        self.fake_ingest_service.ingest_document.assert_not_called()

    def test_ingest_page_no_api_key_returns_409(self) -> None:
        """F6-2：已认证但未配置 API Key → 409 API_KEY_NOT_CONFIGURED，不进入 service。"""
        self.fake_plugin_service.decrypt_api_key.side_effect = (
            ApiKeyNotConfiguredError("current plugin has no api key")
        )

        response = self._post({"page_id": 1, "chunks": ["x"]})

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertIn("API Key", body.get("detail", ""))
        self.assertEqual(body.get("type"), "ApiKeyNotConfiguredError")
        # 未配置 Key 时不得调用 Embedding / Milvus（service 不被调用）
        self.fake_ingest_service.ingest_document.assert_not_called()

    def test_ingest_page_passes_plugin_id_and_api_key(self) -> None:
        """F6-3：已配置 → service 收到 current_plugin.plugin_id 与 decrypt 后的 Key。"""
        self.fake_plugin_service.decrypt_api_key.return_value = "sk-real-plugin"

        response = self._post({"page_id": 5, "chunks": ["x"]})

        self.assertEqual(response.status_code, 200)
        self.fake_plugin_service.decrypt_api_key.assert_called_once_with(
            self.fake_plugin
        )
        self.fake_ingest_service.ingest_document.assert_awaited_once_with(
            document_id=5,
            chunks=["x"],
            plugin_id="plugin-42",
            api_key="sk-real-plugin",
        )

    def test_ingest_page_never_uses_server_key(self) -> None:
        """F6-4：service 收到的 api_key 只能是插件工作空间 Key（服务器 .env Key 不被使用）。"""
        # 服务器 Key 只存在于 settings（测试中未配置）；
        # 断言 service 收到的 api_key 严格等于 decrypt_api_key 的返回值，
        # 且不存在任何 settings.bailian_api_key 注入路径。
        self.fake_plugin_service.decrypt_api_key.return_value = "sk-plugin-only"

        response = self._post({"page_id": 9, "chunks": ["y"]})

        self.assertEqual(response.status_code, 200)
        self.fake_ingest_service.ingest_document.assert_awaited_once_with(
            document_id=9,
            chunks=["y"],
            plugin_id="plugin-42",
            api_key="sk-plugin-only",
        )


if __name__ == "__main__":
    unittest.main()
