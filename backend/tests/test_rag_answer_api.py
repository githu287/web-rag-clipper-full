"""
POST /rag/ask API 集成测试（Phase 3.3 Step 3）。

技术栈：unittest + unittest.mock + FastAPI TestClient。
不连接真实 MySQL / Milvus / 百炼：
    - 通过 app.dependency_overrides 将 get_rag_answer_service 替换为 fake service
      （AsyncMock(ask)），聚焦「路由 + HTTP 映射 + response schema」；
    - 通过 patch("backend.main.get_milvus_initializer") 阻断 lifespan 启动期
      真实 Milvus 连接。

覆盖场景（对应 Phase 3.3 Step 3 §二十二 Rag API 测试要求）：
    1. /rag/ask 200
    2. query 空 → 422
    3. document_id <= 0 → 422
    4. 404
    5. 409
    6. 502
    7. 无结果 200
    8. response schema（answer + sources；extra=forbid）
    + request extra field → 422
    + document_id 正确透传（含 None 缺省）
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.api.deps import get_current_plugin
from backend.clients.llm import LLMClientRequestError
from backend.core.di import get_plugin_service, get_rag_answer_service
from backend.core.exceptions import DocumentNotFoundError, DocumentNotSuccessError
from backend.main import create_app
from backend.models.api_schema import RagAnswerSource, RagAskResponse


class RagAskApiTest(unittest.TestCase):
    """POST /rag/ask API 集成测试。"""

    def setUp(self) -> None:
        """构造隔离 app + fake answer service + 阻断 lifespan Milvus 连接。"""
        self._milvus_init_patcher = patch("backend.main.get_milvus_initializer")
        self.mock_initializer = Mock()
        self.mock_initializer.initialize.return_value = None
        self._milvus_init_patcher.start().return_value = self.mock_initializer

        self.fake_answer_service = Mock()
        self.fake_answer_service.ask = AsyncMock()

        self.app = create_app()
        self.app.dependency_overrides[get_rag_answer_service] = (
            lambda: self.fake_answer_service
        )
        # Phase 3.5 Step 2-E：/rag/ask 需插件认证 + 插件工作空间 API Key 注入
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
        # raise_server_exceptions=False：ResponseValidationError（响应契约破坏）应作为
        # 500 响应返回，而非在 TestClient 侧直接抛异常。
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self._milvus_init_patcher.stop()
        self.app.dependency_overrides.clear()

    def _make_response(
        self,
        answer: str = "这是 AI 生成的回答。",
        sources: list[RagAnswerSource] | None = None,
    ) -> RagAskResponse:
        if sources is None:
            sources = [
                RagAnswerSource(
                    document_id=53,
                    title="测试标题",
                    url="https://example.com/page",
                    chunk_id="53_0",
                    score=0.87,
                )
            ]
        return RagAskResponse(answer=answer, sources=sources)

    # ----------------------------------------------------- 1. /rag/ask 200
    def test_ask_200(self) -> None:
        """1：HTTP 200，answer + sources 正常返回。"""
        self.fake_answer_service.ask.return_value = self._make_response()

        resp = self.client.post("/rag/ask", json={"query": "这篇文章主要讲了什么？", "document_id": 53})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["answer"], "这是 AI 生成的回答。")
        self.assertEqual(len(body["sources"]), 1)
        self.assertEqual(body["sources"][0]["document_id"], 53)
        self.assertEqual(body["sources"][0]["chunk_id"], "53_0")
        self.assertEqual(body["sources"][0]["score"], 0.87)

    # ----------------------------------------------------- document_id 透传
    def test_ask_passes_document_id(self) -> None:
        """document_id 正确透传给 service.ask。"""
        self.fake_answer_service.ask.return_value = self._make_response()

        self.client.post("/rag/ask", json={"query": "问题", "document_id": 53})

        self.fake_answer_service.ask.assert_awaited_once_with(
            query="问题",
            document_id=53,
            plugin_id="plugin-42",  # Phase 3.5 Step 2-E：归属 = 当前插件工作空间
            api_key="sk-plugin",
        )

    def test_ask_document_id_default_none(self) -> None:
        """document_id 缺省时透传 None（全库模式）。"""
        self.fake_answer_service.ask.return_value = self._make_response()

        self.client.post("/rag/ask", json={"query": "问题"})

        self.fake_answer_service.ask.assert_awaited_once_with(
            query="问题",
            document_id=None,
            plugin_id="plugin-42",  # Phase 3.5 Step 2-E：归属 = 当前插件工作空间
            api_key="sk-plugin",
        )

    # ----------------------------------------------------- 2. query 空 → 422
    def test_ask_empty_query_422(self) -> None:
        """2：query 为空 → 422，service 不被调用。"""
        resp = self.client.post("/rag/ask", json={"query": ""})

        self.assertEqual(resp.status_code, 422)
        self.fake_answer_service.ask.assert_not_awaited()

    def test_ask_missing_query_422(self) -> None:
        """query 字段缺失 → 422。"""
        resp = self.client.post("/rag/ask", json={})

        self.assertEqual(resp.status_code, 422)
        self.fake_answer_service.ask.assert_not_awaited()

    # ----------------------------------------------------- 3. document_id <= 0 → 422
    def test_ask_document_id_zero_422(self) -> None:
        """3a：document_id=0 → 422。"""
        resp = self.client.post("/rag/ask", json={"query": "问题", "document_id": 0})

        self.assertEqual(resp.status_code, 422)
        self.fake_answer_service.ask.assert_not_awaited()

    def test_ask_document_id_negative_422(self) -> None:
        """3b：document_id=-1 → 422。"""
        resp = self.client.post("/rag/ask", json={"query": "问题", "document_id": -1})

        self.assertEqual(resp.status_code, 422)
        self.fake_answer_service.ask.assert_not_awaited()

    # ----------------------------------------------------- request extra field → 422
    def test_ask_extra_request_field_422(self) -> None:
        """extra request 字段 → 422（extra=forbid）。"""
        resp = self.client.post(
            "/rag/ask", json={"query": "问题", "document_id": 53, "extra": 1}
        )

        self.assertEqual(resp.status_code, 422)
        self.fake_answer_service.ask.assert_not_awaited()

    # ----------------------------------------------------- 4. 404
    def test_ask_404(self) -> None:
        """4：Document 不存在 → 404，type=DocumentNotFoundError。"""
        self.fake_answer_service.ask.side_effect = DocumentNotFoundError(
            "Document 53 不存在"
        )

        resp = self.client.post("/rag/ask", json={"query": "问题", "document_id": 53})

        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["type"], "DocumentNotFoundError")

    # ----------------------------------------------------- 5. 409
    def test_ask_409(self) -> None:
        """5：Document 非 SUCCESS → 409，type=DocumentNotSuccessError。"""
        self.fake_answer_service.ask.side_effect = DocumentNotSuccessError(
            "Document 53 状态为 FAILED"
        )

        resp = self.client.post("/rag/ask", json={"query": "问题", "document_id": 53})

        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["type"], "DocumentNotSuccessError")

    # ----------------------------------------------------- 6. 502
    def test_ask_502(self) -> None:
        """6：LLM 异常 → 502，type=LLMClientRequestError。"""
        self.fake_answer_service.ask.side_effect = LLMClientRequestError("timeout")

        resp = self.client.post("/rag/ask", json={"query": "问题", "document_id": 53})

        self.assertEqual(resp.status_code, 502)
        body = resp.json()
        self.assertEqual(body["type"], "LLMClientRequestError")

    # ----------------------------------------------------- 7. 无结果 200
    def test_ask_no_result_200(self) -> None:
        """7：无检索结果 → 200 + 固定 answer + 空 sources。"""
        self.fake_answer_service.ask.return_value = self._make_response(
            answer="当前内容中没有足够信息回答该问题。", sources=[]
        )

        resp = self.client.post("/rag/ask", json={"query": "不存在的内容"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["answer"], "当前内容中没有足够信息回答该问题。")
        self.assertEqual(body["sources"], [])

    # ----------------------------------------------------- 8. response schema
    def test_ask_response_schema(self) -> None:
        """8：response schema 恰好 {answer, sources}；source 字段恰好 5 个。"""
        self.fake_answer_service.ask.return_value = self._make_response()

        resp = self.client.post("/rag/ask", json={"query": "问题", "document_id": 53})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body.keys()), {"answer", "sources"})
        self.assertEqual(
            set(body["sources"][0].keys()),
            {"document_id", "title", "url", "chunk_id", "score"},
        )

    def test_ask_response_extra_field_rejected(self) -> None:
        """response 不允许 extra 字段（extra=forbid，契约保护）。"""
        # 模拟 service 返回带非法字段的 dict 时，FastAPI 响应校验应拒绝
        bad_response = {
            "answer": "x",
            "sources": [],
            "unexpected": 1,
        }
        self.fake_answer_service.ask.return_value = bad_response

        resp = self.client.post("/rag/ask", json={"query": "问题"})

        self.assertEqual(resp.status_code, 500)

    def test_ask_response_source_missing_field_rejected(self) -> None:
        """source 缺少必需字段（如 score）→ 响应校验拒绝。"""
        bad_response = {
            "answer": "x",
            "sources": [{"document_id": 1, "chunk_id": "1_0"}],
        }
        self.fake_answer_service.ask.return_value = bad_response

        resp = self.client.post("/rag/ask", json={"query": "问题"})

        self.assertEqual(resp.status_code, 500)


if __name__ == "__main__":
    unittest.main()
