"""
RAG Router：RAG 检索 + RAG 问答 HTTP 接口（Phase 2.7 Step 2；Phase 3.3 Step 3 增 /rag/ask）。

路由：
    POST /rag/search — RAG 检索（保留原契约，行为不变）
    POST /rag/ask    — RAG 问答（Phase 3.3 Step 3 新增）

依赖注入：
    - 每个端点先 Depends(get_current_user) 解析当前用户（Phase 3.4 Step 4）；
    - /rag/search 通过 Depends(get_rag_service) 获取 RagService 实例；
    - /rag/ask    通过 Depends(get_rag_answer_service) 获取 RagAnswerService 实例；
    - /rag/search + /rag/ask 通过 Depends(get_user_service) 解密用户 API Key。

职责边界：
    - 接收 Pydantic 校验后的请求体；
    - 解析当前用户身份（Bearer token）并解密其百炼 API Key；
    - 调用对应 Service 方法（user_id + api_key 透传，实现 ownership + 按用户
      Key 注入 embedding / LLM）；
    - 将结果包装为响应 DTO 返回；
    - 不直接接触 EmbeddingClient / MilvusRepository / LLM / pymilvus / openai。

异常处理：
    Service 抛出的 MilvusRepositoryError / EmbeddingClientError / LLMClientError /
    DocumentNotFoundError / DocumentNotSuccessError 由 main.py 全局 exception_handler
    统一转换为 HTTPException；AuthenticationError（token 无效）→ 401；
    本 Router 不在路由内 try/except 吞异常。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from ...core.di import get_rag_answer_service, get_rag_service, get_user_service
from ...models.api_schema import (
    RagAskRequest,
    RagAskResponse,
    RagSearchRequest,
    RagSearchResponse,
)
from ...models.user import User
from ...services.rag import RagService
from ...services.rag_answer import RagAnswerService
from ...services.user_service import UserService
from ..deps import get_current_user

# 创建 Router（prefix + tags 严格按 Phase 2.7 Step 2 要求）
router: APIRouter = APIRouter(
    prefix="/rag",
    tags=["rag"],
)


@router.post(
    "/search",
    response_model=RagSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="RAG 检索",
    description="用户 query → embedding → Milvus 候选（candidate_limit=max(limit,10)）→ Document SUCCESS status post-filter（含孤儿 chunk 过滤）→ top-K 结果",
)
async def rag_search(
    request: RagSearchRequest,
    current_user: User = Depends(get_current_user),
    service: RagService = Depends(get_rag_service),
    user_service: UserService = Depends(get_user_service),
) -> RagSearchResponse:
    """
    POST /rag/search

    业务流程（Phase 2.3 §7.2 + Phase 3.4 Step 4）：
        1) current_user 由 Bearer token 解析（无效 → 401）；
        2) api_key = user_service.decrypt_api_key(current_user)（用户自己的百炼 Key）；
        3) service.search(query, limit, user_id=current_user.id, api_key=api_key)：
           - query → EmbeddingClient.embed([query], api_key=api_key) → vector
           - Milvus search(vector, limit=candidate_limit=max(limit,10)) → 候选
           - DocumentRepository.get_documents_by_ids(page_ids, user_id) SQL 层
             ownership 过滤（跨用户候选不会进入 SUCCESS 集合）
           - 截取前 limit 条返回

    Args:
        request: RagSearchRequest（query 非空；limit 范围 1-20；默认 5）。
        current_user: 当前登录用户（Bearer token → User；Phase 3.4 Step 4）。
        service: 通过 DI 注入的 RagService 实例。
        user_service: 通过 DI 注入的 UserService（解密当前用户的 API Key）。

    Returns:
        RagSearchResponse: results 字段为 list[RagSearchResult]（按 COSINE similarity 降序，
        最相似在前；每个结果含 Document metadata：document_id / filename / status / created_at）。

    Raises:
        AuthenticationError: token 缺失 / 无效（401）。
        SecurityDecryptionError: API Key 解密失败（500）。
        MilvusRepositoryError: Milvus 检索失败（由全局 handler 转 HTTPException）。
        EmbeddingClientError: 百炼 Embedding 调用失败（由全局 handler 转 HTTPException）。
    """
    api_key = user_service.decrypt_api_key(current_user)
    results = await service.search(
        query=request.query,
        limit=request.limit,
        document_id=request.document_id,
        user_id=current_user.id,
        api_key=api_key,
    )
    return RagSearchResponse(results=results)


@router.post(
    "/ask",
    response_model=RagAskResponse,
    status_code=status.HTTP_200_OK,
    summary="RAG 问答",
    description="用户问题 → Retrieval（经 RagService）→ Context → 百炼 qwen-plus → Answer + Sources；document_id 指定=当前网页模式，缺省=全部知识库模式",
)
async def rag_ask(
    request: RagAskRequest,
    current_user: User = Depends(get_current_user),
    service: RagAnswerService = Depends(get_rag_answer_service),
    user_service: UserService = Depends(get_user_service),
) -> RagAskResponse:
    """
    POST /rag/ask（Phase 3.3 Step 3；Phase 3.4 Step 4 接入用户身份 + 用户 Key）

    业务流程：
        1) current_user 由 Bearer token 解析（无效 → 401）；
        2) api_key = user_service.decrypt_api_key(current_user)（用户自己的百炼 Key）；
        3) RagAnswerService.ask(query, document_id, user_id=current_user.id,
           api_key=api_key) 内部执行：
           - 若指定 document_id：DocumentRepository.get_document(document_id,
             user_id) 校验存在 + ownership + status == SUCCESS
             （不存在 / 跨用户 → 404；非 SUCCESS → 409）；
           - rag_service.search(query, limit=top_k, document_id=document_id,
             user_id=user_id, api_key=api_key)（RAG 检索，SQL 层 ownership 过滤）；
           - 无检索结果 → 返回固定提示（不调用 LLM，HTTP 200）；
           - 构造 Context（[Source N] 块，≤ top_k 个，max 4000 字符截断）→ Prompt →
             LLMClient.generate(system_prompt, user_prompt, api_key=api_key) → answer；
           - 组装 sources（document_id / title / url / chunk_id / score=similarity）。

    Args:
        request: RagAskRequest（query 非空；document_id 可选 > 0）。
        current_user: 当前登录用户（Bearer token → User；Phase 3.4 Step 4）。
        service: 通过 DI 注入的 RagAnswerService 实例。
        user_service: 通过 DI 注入的 UserService（解密当前用户的 API Key）。

    Returns:
        RagAskResponse: answer（LLM 回答或固定提示）+ sources（引用来源列表）。

    Raises:
        AuthenticationError: token 缺失 / 无效（401）。
        SecurityDecryptionError: API Key 解密失败（500）。
        DocumentNotFoundError: 指定 document_id 且 Document 不存在 / 跨用户（→ 404）。
        DocumentNotSuccessError: 指定 document_id 且 Document 状态非 SUCCESS（→ 409）。
        LLMClientError: LLM 生成失败（→ 502）。
        MilvusRepositoryError / EmbeddingClientError: Retrieval 链路失败（→ 503 / 502）。
    """
    api_key = user_service.decrypt_api_key(current_user)
    return await service.ask(
        query=request.query,
        document_id=request.document_id,
        user_id=current_user.id,
        api_key=api_key,
    )
