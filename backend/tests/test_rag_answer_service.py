"""
RagAnswerService 单元测试（Phase 3.3 Step 3）。

技术栈：unittest + unittest.mock。
不连接真实 MySQL / Milvus / 百炼：
    - RagService / LLMClient / DocumentRepository 全部以 Mock 注入（Protocol 可 Mock）；
    - 验证 ask() 的编排顺序：document_id 校验 → 经 RagService 检索 → Context/Prompt
      构造 → LLM → sources 组装。

覆盖场景（对应 Phase 3.3 Step 3 §二十二 RagAnswerService 测试要求）：
    1. 正常回答
    2. document_id 不存在
    3. document 非 SUCCESS
    4. 无 retrieval
    5. 不调用 LLM
    6. LLM error
    7. source 正确
    8. context 正确
    9. prompt 正确
    10. 当前网页模式
    11. 全库模式
    12. document_id 正确透传
    13. context max chars
    14. top_k
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock

from backend.clients.llm import (
    LLMClient,
    LLMClientRequestError,
)
from backend.core.exceptions import DocumentNotFoundError, DocumentNotSuccessError
from backend.models.api_schema import RagAnswerSource, RagAskResponse, RagSearchResult
from backend.models.document import Document, DocumentStatus
from backend.repositories.mysql import DocumentRepository
from backend.services.rag import RagService
from backend.services.rag_answer import RagAnswerService


def _make_document(
    doc_id: int = 53,
    status: str = DocumentStatus.SUCCESS,
    title: str | None = "测试标题",
    url: str | None = "https://example.com/page",
) -> Document:
    return Document(
        id=doc_id,
        user_id=None,
        filename="clip.html",
        file_path="/tmp/clip.html",
        status=status,
        chunk_count=3,
        file_size=1024,
        mime_type="text/html",
        error_message=None,
        title=title,
        url=url,
        source_type="webpage",
        created_at=datetime(2026, 8, 23, 10, 0, 0),
        updated_at=datetime(2026, 8, 23, 10, 0, 0),
    )


def _make_result(
    pk: str = "53_0",
    page_id: int = 53,
    chunk_index: int = 0,
    chunk_text: str = "这是一段网页正文内容。",
    distance: float = 0.87,
    document_id: int = 53,
    title: str | None = "测试标题",
    url: str | None = "https://example.com/page",
) -> RagSearchResult:
    return RagSearchResult(
        id=pk,
        page_id=page_id,
        chunk_index=chunk_index,
        chunk_text=chunk_text,
        distance=distance,
        document_id=document_id,
        filename="clip.html",
        status=DocumentStatus.SUCCESS,
        created_at=datetime(2026, 8, 23, 10, 0, 0),
        title=title,
        url=url,
        source_type="webpage",
    )


class RagAnswerServiceTest(unittest.TestCase):
    """RagAnswerService.ask() 编排单元测试。"""

    def setUp(self) -> None:
        """构造三个 Mock 依赖 + 被测服务实例。"""
        self.rag_service = Mock(spec=RagService)
        self.rag_service.search = AsyncMock()
        self.llm_client = Mock(spec=LLMClient)
        self.llm_client.generate.return_value = "这是 AI 生成的回答。"
        self.document_repository = Mock(spec=DocumentRepository)

        self.service = RagAnswerService(
            rag_service=self.rag_service,
            llm_client=self.llm_client,
            document_repository=self.document_repository,
        )

    # ----------------------------------------------------- 1. 正常回答
    def test_ask_success_returns_answer_and_sources(self) -> None:
        """1+7：正常回答：answer + sources 完整；source 字段来自真实 retrieval result。"""
        result = _make_result()
        self.rag_service.search.return_value = [result]
        self.document_repository.get_document.return_value = _make_document()

        response = self.ask("这篇文章主要讲了什么？", document_id=53)

        self.assertIsInstance(response, RagAskResponse)
        self.assertEqual(response.answer, "这是 AI 生成的回答。")
        self.assertEqual(len(response.sources), 1)
        source = response.sources[0]
        self.assertIsInstance(source, RagAnswerSource)
        self.assertEqual(source.document_id, 53)
        self.assertEqual(source.title, "测试标题")
        self.assertEqual(source.url, "https://example.com/page")
        self.assertEqual(source.chunk_id, "53_0")
        self.assertEqual(source.score, 0.87)

    # ----------------------------------------------------- 2. document_id 不存在
    def test_ask_document_not_found_raises(self) -> None:
        """2：指定 document_id 且 Document 不存在 → DocumentNotFoundError，不调 LLM。"""
        self.document_repository.get_document.side_effect = DocumentNotFoundError(
            "Document 53 不存在"
        )

        with self.assertRaises(DocumentNotFoundError):
            self.ask("问题", document_id=53)

        self.llm_client.generate.assert_not_called()
        self.rag_service.search.assert_not_awaited()

    # ----------------------------------------------------- 3. document 非 SUCCESS
    def test_ask_document_not_success_raises(self) -> None:
        """3：Document 状态非 SUCCESS（FAILED）→ DocumentNotSuccessError。"""
        self.document_repository.get_document.return_value = _make_document(
            status=DocumentStatus.FAILED
        )

        with self.assertRaises(DocumentNotSuccessError):
            self.ask("问题", document_id=53)

        self.llm_client.generate.assert_not_called()

    # ----------------------------------------------------- 4. 无 retrieval
    def test_ask_no_retrieval_returns_empty_answer(self) -> None:
        """4：无检索结果 → 固定提示 + 空 sources，HTTP 层为 200。"""
        self.rag_service.search.return_value = []
        self.document_repository.get_document.return_value = _make_document()

        response = self.ask("问题", document_id=53)

        self.assertEqual(response.answer, "当前内容中没有足够信息回答该问题。")
        self.assertEqual(response.sources, [])
        # 5. 不调用 LLM
        self.llm_client.generate.assert_not_called()

    # ----------------------------------------------------- 5. 不调用 LLM（由用例 4 覆盖）
    def test_ask_skip_llm_when_no_result(self) -> None:
        """5：无结果时不调用 LLM（独立断言，防止回归）。"""
        self.rag_service.search.return_value = []
        self.document_repository.get_document.return_value = _make_document()

        self.ask("问题", document_id=53)

        self.llm_client.generate.assert_not_called()

    # ----------------------------------------------------- 6. LLM error
    def test_ask_llm_error_propagates(self) -> None:
        """6：LLM 异常向上传播（不吞异常）。"""
        self.rag_service.search.return_value = [_make_result()]
        self.document_repository.get_document.return_value = _make_document()
        self.llm_client.generate.side_effect = LLMClientRequestError("timeout")

        with self.assertRaises(LLMClientRequestError):
            self.ask("问题", document_id=53)

    # ----------------------------------------------------- 8. context 正确
    def test_ask_context_contains_chunk_and_metadata(self) -> None:
        """8：user_prompt 包含 chunk_text / title / url / document_id / 用户问题。"""
        result = _make_result(chunk_text="独特正文内容 ABC")
        self.rag_service.search.return_value = [result]
        self.document_repository.get_document.return_value = _make_document()

        self.ask("这篇文章主要讲了什么？", document_id=53)

        _, kwargs = self.llm_client.generate.call_args
        user_prompt = kwargs["user_prompt"] if "user_prompt" in kwargs else (
            self.llm_client.generate.call_args.args[1]
        )
        self.assertIn("独特正文内容 ABC", user_prompt)
        self.assertIn("测试标题", user_prompt)
        self.assertIn("https://example.com/page", user_prompt)
        self.assertIn("document_id: 53", user_prompt)
        self.assertIn("用户问题：", user_prompt)
        self.assertIn("这篇文章主要讲了什么？", user_prompt)

    # ----------------------------------------------------- 9. prompt 正确
    def test_ask_system_prompt_contains_constraints(self) -> None:
        """9：system_prompt 包含关键回答约束（只能依据 Context / 禁止编造等）。"""
        self.rag_service.search.return_value = [_make_result()]
        self.document_repository.get_document.return_value = _make_document()

        self.ask("问题", document_id=53)

        call_args = self.llm_client.generate.call_args
        # generate(system_prompt, user_prompt) 位置参数或关键字参数
        system_prompt = call_args.kwargs.get("system_prompt") or call_args.args[0]
        for keyword in [
            "只能依据",
            "Context",
            "不得使用模型自身知识",
            "没有答案",
            "明确说明",
            "禁止编造",
            "禁止猜测",
            "[Source",
        ]:
            self.assertIn(keyword, system_prompt)

    # ----------------------------------------------------- 10+12. 当前网页模式 + 透传
    def test_ask_current_document_mode_passes_document_id(self) -> None:
        """10+12：当前网页模式：document_id 正确透传给 rag_service.search。"""
        self.rag_service.search.return_value = [_make_result()]
        self.document_repository.get_document.return_value = _make_document()

        self.ask("问题", document_id=53)

        self.rag_service.search.assert_awaited_once_with(
            query="问题",
            limit=5,
            document_id=53,
            user_id=None,  # Phase 3.4 Step 4：user_id/api_key 透传（Service 层可选）
            api_key=None,
        )

    # ----------------------------------------------------- 11. 全库模式
    def test_ask_full_knowledge_mode_skips_document_check(self) -> None:
        """11：全库模式（document_id=None）：不查 Document、search 不传 document_id。"""
        self.rag_service.search.return_value = [_make_result()]

        self.ask("问题", document_id=None)

        self.document_repository.get_document.assert_not_called()
        self.rag_service.search.assert_awaited_once_with(
            query="问题",
            limit=5,
            document_id=None,
            user_id=None,  # Phase 3.4 Step 4：user_id/api_key 透传（Service 层可选）
            api_key=None,
        )

    # ----------------------------------------------------- 13. context max chars
    def test_ask_context_max_chars_truncated(self) -> None:
        """13：超长 context 被截断到 max_context_chars（不允许无限增长）。"""
        long_text = "长" * 10000
        self.rag_service.search.return_value = [_make_result(chunk_text=long_text)]
        self.document_repository.get_document.return_value = _make_document()
        # 注入更小上限验证截断逻辑
        self.service._max_context_chars = 100

        self.ask("问题", document_id=53)

        call_args = self.llm_client.generate.call_args
        user_prompt = call_args.kwargs.get("user_prompt") or call_args.args[1]
        # Context 部分（"Context:\n..." 到 "用户问题：" 之前）应被截断至 ≤ 上限 + 前缀/分隔符
        # 结构：f"Context:\n{context}\n\n用户问题：\n{query}"，其中 len(context) ≤ 100
        context_section = user_prompt.split("用户问题：")[0]
        max_allowed = len("Context:\n") + 100 + len("\n\n")
        self.assertLessEqual(len(context_section), max_allowed)
        self.assertGreaterEqual(len(context_section), 100)

    # ----------------------------------------------------- 14. top_k
    def test_ask_top_k_limits_sources_and_context(self) -> None:
        """14：top_k 限制进入 Context / Sources 的 chunk 数（防御 search 返回过多）。"""
        results = [_make_result(pk=f"53_{i}", chunk_index=i) for i in range(5)]
        self.rag_service.search.return_value = results
        self.document_repository.get_document.return_value = _make_document()

        self.ask("问题", document_id=53, top_k=3)

        call_args = self.llm_client.generate.call_args
        user_prompt = call_args.kwargs.get("user_prompt") or call_args.args[1]
        context_section = user_prompt.split("用户问题：")[0]
        self.assertIn("[Source 1]", context_section)
        self.assertIn("[Source 3]", context_section)
        self.assertNotIn("[Source 4]", context_section)

    # ----------------------------------------------------- 辅助
    def ask(
        self,
        query: str,
        document_id: int | None = None,
        top_k: int = 5,
        user_id: int | None = None,
        api_key: str | None = None,
    ) -> RagAskResponse:
        """同步测试入口：包装 async ask()。"""
        import asyncio

        return asyncio.run(
            self.service.ask(
                query=query,
                document_id=document_id,
                top_k=top_k,
                user_id=user_id,
                api_key=api_key,
            )
        )

    # ----------------------------------------------------- 额外：service 层空 query 防御
    def test_ask_empty_query_raises_value_error(self) -> None:
        """service 层防御：空 query → ValueError，不触发任何下游。"""
        with self.assertRaises(ValueError):
            self.ask("   ")
        self.rag_service.search.assert_not_awaited()
        self.llm_client.generate.assert_not_called()

    # ----------------------------------------------------- Phase 3.4 Step E：用户隔离链路
    def test_ask_passes_user_id_and_api_key(self) -> None:
        """11：user_id / api_key 正确透传给 rag_service.search（隔离检索前提）。"""
        self.rag_service.search.return_value = [_make_result()]
        self.document_repository.get_document.return_value = _make_document()

        self.ask("问题", document_id=53, user_id=1, api_key="sk-user-1")

        self.rag_service.search.assert_awaited_once_with(
            query="问题",
            limit=5,
            document_id=53,
            user_id=1,
            api_key="sk-user-1",
        )
        # ownership check 也收到 user_id（document_id + user_id 一起判定）
        self.document_repository.get_document.assert_called_once_with(53, 1)

    def test_ask_document_owned_by_other_user_404(self) -> None:
        """12：document 归属其他用户 → DocumentNotFoundError（404），不调 LLM。"""
        self.document_repository.get_document.side_effect = DocumentNotFoundError(
            "Document 53 不存在或不属于当前用户"
        )

        with self.assertRaises(DocumentNotFoundError):
            self.ask("问题", document_id=53, user_id=1)

        self.rag_service.search.assert_not_awaited()
        self.llm_client.generate.assert_not_called()

    def test_ask_full_knowledge_mode_uses_current_user_retrieval(self) -> None:
        """13：全库模式只通过当前用户的 retrieval（user_id 透传 + 无 Document 前置检查）。"""
        self.rag_service.search.return_value = [_make_result()]

        self.ask("问题", document_id=None, user_id=7, api_key="sk-user-7")

        self.document_repository.get_document.assert_not_called()
        self.rag_service.search.assert_awaited_once_with(
            query="问题",
            limit=5,
            document_id=None,
            user_id=7,
            api_key="sk-user-7",
        )

    def test_ask_empty_knowledge_base_skips_llm(self) -> None:
        """14：全库模式空知识库（search 返回 []）→ 固定提示 + 不调用 LLM。"""
        self.rag_service.search.return_value = []

        response = self.ask(
            "问题", document_id=None, user_id=1, api_key="sk-user-1"
        )

        self.assertEqual(response.answer, "当前内容中没有足够信息回答该问题。")
        self.assertEqual(response.sources, [])
        self.llm_client.generate.assert_not_called()

    def test_ask_sources_only_from_current_user_retrieval(self) -> None:
        """15：sources 只来自当前用户 retrieval（search 已隔离 → source 不含他人文档）。"""
        # search 被隔离后只返回当前用户 document_id=53 的结果
        self.rag_service.search.return_value = [_make_result(document_id=53)]
        self.document_repository.get_document.return_value = _make_document()

        response = self.ask("问题", document_id=53, user_id=1)

        self.assertEqual([s.document_id for s in response.sources], [53])
        # 检索确实以当前用户身份执行（隔离前提）
        self.assertEqual(self.rag_service.search.await_args.kwargs["user_id"], 1)

    def test_ask_context_excludes_other_users_chunks(self) -> None:
        """16：context 不包含其他用户 chunk（search 已隔离 → prompt 只含当前用户内容）。"""
        self.rag_service.search.return_value = [
            _make_result(chunk_text="A 用户独有内容，其他用户不可见。")
        ]
        self.document_repository.get_document.return_value = _make_document()

        self.ask("问题", document_id=53, user_id=1)

        call_args = self.llm_client.generate.call_args
        user_prompt = call_args.kwargs.get("user_prompt") or call_args.args[1]
        self.assertIn("A 用户独有内容", user_prompt)
        self.assertNotIn("B 用户独有内容", user_prompt)


if __name__ == "__main__":
    unittest.main()
