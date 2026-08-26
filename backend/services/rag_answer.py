"""
RagAnswerService：RAG 问答编排（Phase 3.3 Step 3）。

职责：
    用户问题 → Retrieval（经 RagService）→ Context 构造 → 百炼 LLM → Answer + Sources

流程（严格按照设计确认）：
    Step 1: 参数防御（query 必须非空）
    Step 2: 若指定 document_id：
              DocumentRepository.get_document(document_id, user_id) 校验存在性
              （不存在 → DocumentNotFoundError → HTTP 404；跨用户同 404）；
              Document.status != SUCCESS → DocumentNotSuccessError → HTTP 409。
              不依赖 Milvus 中是否存在 chunk。
    Step 3: Retrieval —— 调用 rag_service.search(query, limit=top_k,
              document_id=document_id, user_id=user_id, api_key=api_key)
              （不在本 Service 直接操作 Milvus）。
    Step 4: 无 retrieval result → 不调用 LLM，返回固定 answer + 空 sources。
    Step 5: 构造 Context（[Source N] 块；最多 top_k 个；max_context_chars 默认 4000，
              超出后截断，不允许无限增长 / 不允许塞入整个 Document 原文）。
    Step 6: 构造 Prompt（system_prompt 固定模板 + user_prompt = Context + 用户问题）。
    Step 7: llm_client.generate(system_prompt, user_prompt, api_key=api_key)。

Phase 3.4 Step D（user-aware 身份传递）：
    ask() 新增 user_id 与 api_key 参数：
      - user_id：身份传递 + ownership —— get_document(document_id, user_id)
        （跨用户 → DocumentNotFoundError → 404）与 search(user_id) 一致；
      - api_key：LLM 生成使用当前用户自己的百炼 API Key（AuthService
        decrypt_api_key 解密后传入；Phase 3.4 Step F6：必填，严禁回退
        settings.bailian_api_key）。
    绝不使用服务器 Key 冒充用户 Key。
    Step 8: 组装 Sources（document_id / title / url / chunk_id=result.id / score=result.distance；
              不重新转换 score，当前 Milvus COSINE 语义：越大越相似）。

依赖注入：
    - RagService：RAG 检索（唯一 Retrieval 入口）
    - LLMClient（Protocol）：百炼 LLM 生成
    - DocumentRepository（Protocol）：document_id 状态校验

严格不依赖：
    - MilvusRepository / EmbeddingClient（直接访问 Milvus / 百炼 Embedding 属越权，
      由 RagService 统一封装）。

异步说明：
    与 RagService 同模式：本 Service 为 async def（为未来异步契约预留），
    但依赖的 Repository / Client 均为同步实现，方法内部直接同步调用。
"""

from __future__ import annotations

import logging
from typing import Final

from ..clients.llm import LLMClient
from ..core.exceptions import DocumentNotSuccessError
from ..models.api_schema import RagAnswerSource, RagAskResponse
from ..models.document import DocumentStatus
from ..repositories.mysql import DocumentRepository
from ..services.rag import RagService

logger: logging.Logger = logging.getLogger(__name__)

# 无检索结果时的固定回答（不调用 LLM；HTTP 200）
_EMPTY_ANSWER: Final[str] = "当前内容中没有足够信息回答该问题。"

# Context 最大字符数默认值（防 Prompt 无限增长；允许测试注入更小值验证截断）
_DEFAULT_MAX_CONTEXT_CHARS: Final[int] = 4000

# Context 中单块 [Source N] 的分隔符
_SOURCE_SEPARATOR: Final[str] = "\n\n---\n\n"

# system prompt 固定模板（回答约束）
_SYSTEM_PROMPT: Final[str] = (
    "你是一个基于给定 Context 的 RAG 问答助手。请严格遵守以下规则：\n"
    "1. 只能依据 Context 中的内容回答，不得使用模型自身知识替换 Context。\n"
    "2. 如果 Context 中没有答案，必须明确说明，不得编造或猜测。\n"
    "3. 禁止编造事实，禁止猜测来源中没有的内容。\n"
    "4. 可以引用来源编号（如 [Source 1]）。\n"
    "5. 回答简洁清晰、条理分明。\n"
    "6. 如果用户问题与 Context 完全无关，请明确说明。"
)


class RagAnswerService:
    """
    RAG 问答编排服务。

    构造依赖通过 DI 注入（core/di.py get_rag_answer_service() 工厂）：
        rag_service         : RagService            — RAG 检索（唯一 Retrieval 入口）
        llm_client          : LLMClient（Protocol） — 百炼 LLM 生成
        document_repository : DocumentRepository    — document_id 状态校验
    """

    def __init__(
        self,
        rag_service: RagService,
        llm_client: LLMClient,
        document_repository: DocumentRepository,
        max_context_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
    ) -> None:
        """
        注入 RagService + LLMClient + DocumentRepository。

        Args:
            rag_service: RAG 检索服务实例（不允许直接依赖 MilvusRepository）。
            llm_client: LLM 生成客户端实例（Protocol 类型，便于 Mock）。
            document_repository: Document Repository 实例（Protocol 类型，用于
                document_id 存在性与状态校验）。
            max_context_chars: Context 最大字符数（默认 4000；超出后截断）。
        """
        self._rag_service: RagService = rag_service
        self._llm: LLMClient = llm_client
        self._documents: DocumentRepository = document_repository
        self._max_context_chars: int = max_context_chars

    # ------------------------------------------------------------------ 对外入口
    async def ask(
        self,
        query: str,
        document_id: int | None = None,
        top_k: int = 5,
        user_id: int | None = None,
        api_key: str | None = None,
    ) -> RagAskResponse:
        """
        RAG 问答：query → Retrieval → Context → LLM → Answer + Sources。

        Phase 3.4 Step D：新增 user_id 与 api_key —— user_id 用于
        get_document(document_id, user_id) ownership 校验与 search(user_id)
        传递（Router 必传 current_user.id）；api_key 用于 LLM 生成（用户自己
        的 Key，None 回退测试 Key）。

        Args:
            query: 用户问题（非空字符串；API 层 Pydantic min_length=1 已拦截，
                本方法再做一次防御）。
            document_id: 可选；指定后仅基于该 Document（当前网页模式）回答，
                且该 Document 必须存在且状态为 SUCCESS；None = 全部知识库模式。
            top_k: 最终进入 Context / Sources 的 chunk 数上限（默认 5）。
            user_id: 当前用户 ID（Phase 3.4 Step D；ownership，Router 必传；
                默认 None 仅向后兼容旧调用）。
            api_key: 用户自己的百炼 API Key（Phase 3.4 Step D；None 回退测试 Key）。

        Returns:
            RagAskResponse：
                answer  — LLM 回答；无检索结果时为固定提示语（不调用 LLM）。
                sources — 引用来源列表（来自真实 retrieval result；无结果时空列表）。

        Raises:
            ValueError: query 为空字符串（service 层防御）。
            DocumentNotFoundError: 指定 document_id 且 Document 不存在（→ 404）。
            DocumentNotSuccessError: 指定 document_id 且 Document 状态非 SUCCESS（→ 409）。
            EmbeddingClientError / MilvusRepositoryError / DocumentOperationError:
                Retrieval 链路异常（向上传播，不吞异常）。
            LLMClientError: LLM 生成异常（向上传播，不吞异常）。
        """
        # -------------------------------------------------------- Step 1: 参数防御
        if not isinstance(query, str) or not query.strip():
            raise ValueError("RagAnswerService.ask: query 必须为非空字符串")

        # -------------------------------------------------------- Step 2: document_id 校验
        # Phase 3.4 Step D：get_document(document_id, user_id) 完成 ownership
        # 校验（跨用户 → DocumentNotFoundError → 404）
        if document_id is not None:
            document = self._documents.get_document(document_id, user_id)
            if document.status != DocumentStatus.SUCCESS:
                raise DocumentNotSuccessError(
                    f"Document {document_id} 状态为 {document.status}，"
                    "仅 SUCCESS 状态的文档允许回答"
                )

        # -------------------------------------------------------- Step 3: Retrieval（经 RagService）
        results = await self._rag_service.search(
            query=query,
            limit=top_k,
            document_id=document_id,
            user_id=user_id,
            api_key=api_key,
        )
        logger.info(
            "RagAnswerService.ask: query=%r, document_id=%r, top_k=%d, "
            "检索结果=%d",
            query,
            document_id,
            top_k,
            len(results),
        )

        # -------------------------------------------------------- Step 4: 无结果 → 不调用 LLM
        if not results:
            logger.info(
                "RagAnswerService.ask: 无检索结果，跳过 LLM，返回固定提示"
            )
            return RagAskResponse(answer=_EMPTY_ANSWER, sources=[])

        # -------------------------------------------------------- Step 5: 构造 Context
        top_results = results[:top_k]
        context = self._build_context(top_results)

        # -------------------------------------------------------- Step 6: 构造 Prompt
        system_prompt = _SYSTEM_PROMPT
        user_prompt = self._build_user_prompt(context, query)

        # -------------------------------------------------------- Step 7: LLM
        # Phase 3.4 Step D：LLM 生成使用用户自己的 API Key（None 回退测试 Key）
        answer = self._llm.generate(system_prompt, user_prompt, api_key=api_key)

        # -------------------------------------------------------- Step 8: 组装 Sources
        sources = [self._to_source(result) for result in top_results]

        return RagAskResponse(answer=answer, sources=sources)

    # ------------------------------------------------------------------ 内部辅助
    def _build_context(self, results: list) -> str:
        """
        构造 Context 文本（最多 len(results) 个 [Source N] 块）。

        约束：
            - 使用真实 retrieval result（chunk_text / title / url / document_id）；
            - 明确分隔 source（空行 + [Source N] 标记）；
            - 拼接后总长度超出 max_context_chars 时截断（不允许无限增长）；
            - 不允许把整个 Document 原文塞入 prompt（仅 top_k 个 chunk）。

        Args:
            results: 已截取 top_k 的检索结果列表（元素为 RagSearchResult）。

        Returns:
            Context 字符串（长度 ≤ max_context_chars，截断时保留前缀）。
        """
        blocks: list[str] = []
        for idx, result in enumerate(results, start=1):
            title = result.title or ""
            url = result.url or ""
            block = (
                f"[Source {idx}]\n"
                f"title: {title}\n"
                f"url: {url}\n"
                f"document_id: {result.document_id}\n"
                f"chunk:\n"
                f"{result.chunk_text}"
            )
            blocks.append(block)

        context = _SOURCE_SEPARATOR.join(blocks)
        if len(context) > self._max_context_chars:
            context = context[: self._max_context_chars]
        return context

    def _build_user_prompt(self, context: str, query: str) -> str:
        """
        构造 user prompt：Context + 用户问题（结构化拼接，非无边界字符串）。
        """
        return f"Context:\n{context}\n\n用户问题：\n{query}"

    def _to_source(self, result) -> RagAnswerSource:
        """
        将单个检索结果组装为 RagAnswerSource（真实 retrieval 数据，不转换 score）。

        chunk_id = result.id；score = result.distance（Milvus COSINE，越大越相似）。
        document_id 防御性回退到 page_id（document.id = Milvus.page_id 1:1，
        SUCCESS 过滤后 document_id 必有值）。
        """
        return RagAnswerSource(
            document_id=(
                result.document_id
                if result.document_id is not None
                else result.page_id
            ),
            title=result.title,
            url=result.url,
            chunk_id=result.id,
            score=result.distance,
        )
