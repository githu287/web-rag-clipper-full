"""
RagService：RAG 检索编排（Phase 2.6 Step 2；Phase 2.12 Step 2 接入 Document status post-filter；
Phase 2.13 Step 2 为结果附带 Document metadata）。

职责：
    用户 query → embedding → Milvus ANN 检索 → 应用层 status post-filter → 返回 top-K 结果

Phase 2.12 Step 2（Document 生命周期集成）：
    RAG 检索链路由「query → Embedding → Milvus → top-K」升级为：
    query → Embedding → Milvus candidate retrieval → 批量查询 Document →
    SUCCESS-only filter → orphan filter（MySQL 不存在）→ 截取 limit。
    document.id = Milvus page_id（1:1），检索结果中的 page_id 即 document_id。

Phase 2.13 Step 2（RAG Document Metadata Response）：
    过滤逻辑与 Milvus/MySQL 查询次数完全不变（仍只调用一次 get_documents_by_ids），
    仅将反查得到的 Document 元数据（document_id / filename / status / created_at）
    组装进 API 边界 DTO RagSearchResult。关联使用 document_map[id]（ID 映射），
    禁止依赖 get_documents_by_ids 的返回顺序（SQL IN 查询不保证顺序）。

Phase 3.1 Step 3（WebClip metadata）：
    RagSearchResult 新增 title / url / source_type（共 12 字段），数据直接来自
    RagService 已持有的 document_map[page_id] Document 对象，不新增 SQL / N+1；
    RAG filtering / candidate_limit / orphan filter 均未改动。

Phase 3.5 Step 2-E（workspace-aware 身份传递）：
    search() 参数 user_id 已迁移为 plugin_id（与 DocumentRepository
    plugin_id 化一致）：
      - plugin_id：ownership 约束 —— get_documents_by_ids(page_ids, plugin_id)
        在 SQL 层完成 Workspace 归属过滤，跨 Workspace 候选不会进入
        SUCCESS 集合；Router 层必传 current_plugin.plugin_id。
      - api_key：query embedding 使用当前插件工作空间自己的百炼 API Key
        （PluginService.decrypt_api_key 解密后传入；Phase 3.4 Step F6：
        必填，严禁回退 settings.bailian_api_key）。

依赖注入：
    - EmbeddingClient：query 文本 → embedding 向量
    - MilvusRepository（Protocol）：Milvus 向量检索
    - DocumentRepository（Protocol）：MySQL documents 批量反查（status post-filter）

严格不依赖：
    - pymilvus / MilvusClient（由 Repository Impl 内部隔离）
    - openai / OpenAI（由 EmbeddingClient 内部隔离）
    - FastAPI / Request（API 层职责）

异步说明：
    本 Service 使用 async def（为未来异步 Repository/EmbeddingClient 预留接口契约），
    但当前 Repository 与 EmbeddingClient 均为同步实现，方法内部直接同步调用（不使用
    asyncio.to_thread()，不引入异步 SDK，不重构 Repository）。
"""

from __future__ import annotations

import logging

from ..clients.embedding import EmbeddingClient
from ..core.exceptions import DocumentNotFoundError, DocumentNotSuccessError
from ..models.api_schema import RagSearchResult
from ..models.document import DocumentStatus
from ..models.milvus_dto import ChunkSearchResult
from ..repositories.milvus import MilvusRepository
from ..repositories.mysql import DocumentRepository

logger: logging.Logger = logging.getLogger(__name__)

# Milvus 初始候选召回数下限（Phase 2.2 §14.3 / Phase 2.3 §7.1：固定扩大召回 top-10）。
# Phase 2.12 Step 2：实际召回数 = max(limit, 该下限)，保证 status post-filter 后
# 仍有机会凑满用户请求的 limit（limit ∈ [1, 20]）。
_MILVUS_SEARCH_CANDIDATE_LIMIT: int = 10


class RagService:
    """
    RAG 检索编排服务。

    构造依赖通过 DI 注入（core/di.py get_rag_service() 工厂）：
        embedding_client   : EmbeddingClient       — query 文本嵌入
        milvus_repository  : MilvusRepository       — Milvus 向量检索（Protocol 类型）
        document_repository: DocumentRepository     — MySQL documents 批量反查（Protocol 类型）
    """

    def __init__(
        self,
        embedding_client: EmbeddingClient,
        milvus_repository: MilvusRepository,
        document_repository: DocumentRepository,
    ) -> None:
        """
        注入 EmbeddingClient + MilvusRepository + DocumentRepository。

        Args:
            embedding_client: 百炼 Embedding 客户端实例（同步 embed 方法）。
            milvus_repository: Milvus Repository 实例（Protocol 类型，不依赖具体 Impl）。
            document_repository: Document Repository 实例（Protocol 类型，用于
                Milvus 检索后批量反查 Document.status，实现 SUCCESS-only 过滤）。
        """
        self._embedding: EmbeddingClient = embedding_client
        self._repo: MilvusRepository = milvus_repository
        self._documents: DocumentRepository = document_repository

    # ------------------------------------------------------------------ 对外入口
    async def search(
        self,
        query: str,
        limit: int = 5,
        document_id: int | None = None,
        plugin_id: str | None = None,
        api_key: str | None = None,
    ) -> list[RagSearchResult]:
        """
        RAG 检索：query → embedding → Milvus candidate → SUCCESS-only filter → top-K。

        Phase 3.3 Step 3 新增可选参数 document_id（默认 None，行为与之前完全一致）：
            当 document_id is None     → 完全执行原逻辑（candidate_limit = max(limit, 10)）。
            当 document_id is not None → candidate_limit = max(limit * 4, 40)，并在
                SUCCESS / orphan filter 后只保留 candidate.page_id == document_id，
                最后 results[:limit]（当前网页模式）。

        流程（Phase 2.12 Step 2；Phase 2.13 Step 2 增 metadata 组装）：
          Step 1: query → EmbeddingClient.embed([query]) → 取 vector[0]
          Step 2: Milvus search(vector, limit=candidate_limit) → 候选结果列表
                  （candidate_limit = max(limit, 10)；document_id 模式 = max(limit*4, 40)）
          Step 3: 应用层 status post-filter：
                  1) 收集候选 page_id 并去重（保持首次出现顺序）；
                  2) DocumentRepository.get_documents_by_ids(page_ids) 一次批量反查
                     （禁止对每个 chunk 单独查询，无 N+1）；
                  3) 只保留 Document.status == DocumentStatus.SUCCESS 的候选；
                  4) MySQL 中不存在的 page_id（孤儿 chunk）自动被过滤；
          Step 3.5（Phase 2.13 Step 2）: 构建 document_map = {doc.id: doc}，
                  用于按 ID 关联 Document 元数据；禁止依赖 Repository 返回顺序。
          Step 3.6（Phase 3.3 Step 3）: 若指定 document_id，仅保留
                  candidate.page_id == document_id（应用层过滤，不修改 Milvus Schema /
                  MilvusRepository Protocol）。
          Step 4: 截取前 limit 条返回，组装 RagSearchResult（继承 ChunkSearchResult 的
                  5 个检索字段 + document_id / filename / status / created_at）。
                  过滤后不足 limit 时直接返回，不二次召回。

        Phase 3.5 Step 2-E：参数 user_id 已迁移为 plugin_id（与 DocumentRepository
        契约一致）——plugin_id 用于 get_documents_by_ids(page_ids, plugin_id) 的
        SQL 层归属过滤（workspace ownership 约束，Router 必传）；api_key 用于
        query embedding（当前插件工作空间自己的 Key，None 回退测试 Key）。

        RAG 检索 Workspace 隔离（Phase 3.4 Step E 的 user 隔离迁移为 plugin 隔离）：
            - 全部知识库模式（document_id is None）：
                1) plugin_id 为 None → 禁止匿名访问，直接返回 []（绝不查库）；
                2) success_ids = DocumentRepository.get_success_document_ids(plugin_id)
                   （SQL 层 plugin_id + SUCCESS 双条件过滤；严禁先查全部文档再 Python 过滤）；
                3) success_ids 为空 → 返回 []，不调用 Milvus；
                4) Milvus search 携带 expr = "page_id in [...]"（Milvus 侧收敛召回）；
                5) 应用层最终兜底 candidate.page_id in success_ids（双保险）。
            - 当前网页模式（document_id is not None）：
                1) get_document(document_id, plugin_id) 前置校验：不存在 / 跨 Workspace
                   → DocumentNotFoundError（404）；非 SUCCESS → DocumentNotSuccessError（409）；
                2) Milvus search 携带 expr = "page_id == {document_id}"；
                3) 应用层最终兜底 candidate.page_id == document_id。
            权限判断必须是 document_id + plugin_id 同时进入 Repository ownership check，
            严禁仅凭 document_id 判断。

        Args:
            query: 用户查询文本（非空字符串）。
            limit: 最终返回结果数上限；默认 5（硬编码；.env 的 RAG_TOP_K 为未来预留配置，
                   当前不读取），范围 [1, 20]。
            document_id: 可选；指定后仅返回该 Document（page_id）的检索结果
                （当前网页模式，需归属当前插件工作空间）；None = 全部知识库模式。
            plugin_id: 当前插件工作空间 ID（Phase 3.5 Step 2-E；workspace 隔离；
                全库模式必传，None 时全库模式直接返回 []，禁止匿名访问）。
            api_key: 插件工作空间自己的百炼 API Key（Phase 3.4 Step D；None 回退测试 Key）。

        Returns:
            按 COSINE similarity 降序（最相似在前；保持 Milvus 返回顺序，不做二次排序）
            的 RagSearchResult 列表，长度 ≤ limit。
            每个结果含 Document 元数据（document_id = page_id；filename / status /
            created_at / title / url / source_type 来自 MySQL）；orphan / 非 SUCCESS
            文档已被过滤。
            空列表 = 无 SUCCESS 检索结果（或 Milvus 无候选）。

        Raises:
            EmbeddingClientError: 百炼 Embedding 调用失败（向上传播，不吞异常）。
            MilvusRepositoryError: Milvus 检索失败（向上传播，不吞异常）。
            DocumentOperationError: MySQL 批量反查失败（向上传播，不吞异常；
                main.py 已映射 HTTP 503，禁止伪装为空结果）。
        """
        # ---------------------------------------------------------- Step 1: query → embedding
        # Phase 3.4 Step D：query 向量使用用户自己的 API Key（None 回退测试 Key）
        vectors: list[list[float]] = self._embedding.embed([query], api_key=api_key)
        query_vector: list[float] = vectors[0]

        # ---------------------------------------------------------- Step 2: Milvus candidate retrieval
        # Phase 3.4 Step E：RAG 检索按用户隔离，构造 Milvus expr。
        if document_id is not None:
            # 当前网页模式：document_id + plugin_id 一起进入 ownership check（Step C 已
            # 实现 get_document(document_id, plugin_id) SQL 层归属过滤）。
            document = self._documents.get_document(document_id, plugin_id)
            if document is None:
                raise DocumentNotFoundError(
                    f"Document {document_id} 不存在或不属于当前插件工作空间"
                )
            if document.status != DocumentStatus.SUCCESS:
                raise DocumentNotSuccessError(
                    f"Document {document_id} 当前状态为 {document.status}，"
                    "非 SUCCESS 不可检索"
                )
            candidate_limit: int = max(limit * 4, 40)
            expr: str = f"page_id == {document_id}"
        else:
            # 全部知识库模式：禁止匿名访问——plugin_id 只能来自后端认证上下文，
            # 绝不允许客户端传入。None 时直接返回 []（不查库、不调用 Milvus）。
            if plugin_id is None:
                logger.warning(
                    "RagService.search: 全部知识库模式缺少 plugin_id，"
                    "拒绝匿名访问并返回空结果"
                )
                return []
            # success_ids 必须来自 Repository 的 get_success_document_ids（SQL 层
            # plugin_id + SUCCESS 双条件过滤，严禁先查全部文档再 Python 过滤）。
            success_ids: list[int] = self._documents.get_success_document_ids(plugin_id)
            if not success_ids:
                logger.info(
                    "RagService.search: 插件工作空间 %s 无 SUCCESS document，"
                    "返回空结果（不调用 Milvus）",
                    plugin_id,
                )
                return []
            candidate_limit = max(limit, _MILVUS_SEARCH_CANDIDATE_LIMIT)
            expr = f"page_id in [{','.join(map(str, success_ids))}]"

        candidates: list[ChunkSearchResult] = self._repo.search(
            query_vector,
            limit=candidate_limit,
            expr=expr,
        )
        logger.info(
            "RagService.search: query=%r, document_id=%r, Milvus 候选召回数=%d, "
            "candidate_limit=%d, expr=%r, 最终 limit=%d",
            query,
            document_id,
            len(candidates),
            candidate_limit,
            expr,
            limit,
        )

        # ---------------------------------------------------------- Step 3: SUCCESS-only post-filter
        if not candidates:
            return []

        # 3.1 收集 page_id 并去重（dict.fromkeys 保持首次出现顺序，便于测试断言）。
        page_ids: list[int] = list(dict.fromkeys(c.page_id for c in candidates))

        # 3.2 一次批量反查 Document（空 page_ids 已被上方 candidates 空判断短路）。
        # Phase 3.5 Step 2-E：SQL 层 plugin_id 归属过滤（契约已 plugin_id 化）。
        documents = self._documents.get_documents_by_ids(page_ids, plugin_id)

        # 3.3 只保留 SUCCESS；FAILED / DELETING / PENDING / PROCESSING 全部过滤；
        #     不存在的 Document（孤儿 chunk）因不在 success 集合中也被过滤。
        #     用 {doc.id: doc} 建立 ID 映射（Phase 2.13 Step 2）：
        #     - get_documents_by_ids 的 SQL IN 查询不保证返回顺序，禁止用列表位置关联；
        #     - 后续组装 metadata 时按 document_map[c.page_id] 取值。
        document_map = {document.id: document for document in documents}
        success_document_ids = {
            document.id
            for document in documents
            if document.status == DocumentStatus.SUCCESS
        }
        filtered = [
            c for c in candidates if c.page_id in success_document_ids
        ]
        logger.info(
            "RagService.search: page_ids=%r, SUCCESS 文档=%r, "
            "过滤后候选=%d",
            page_ids,
            sorted(success_document_ids),
            len(filtered),
        )

        # ---------------------------------------------------------- Step 3.6: Workspace 隔离最终兜底（Phase 3.5 Step 2-E）
        # 应用层双保险（Milvus expr 之外的最终防线）：
        #   - 当前网页模式：仅保留 candidate.page_id == document_id；
        #   - 全部知识库模式：仅保留 candidate.page_id in success_ids
        #     （即使 Milvus / MySQL 出现异常返回，也绝不允许跨 Workspace 候选泄漏）。
        if document_id is not None:
            filtered = [c for c in filtered if c.page_id == document_id]
            logger.info(
                "RagService.search: document_id=%r 过滤后候选=%d",
                document_id,
                len(filtered),
            )
        else:
            filtered = [c for c in filtered if c.page_id in success_ids]
            logger.info(
                "RagService.search: 插件工作空间 %s success_ids=%r 最终兜底后候选=%d",
                plugin_id,
                success_ids,
                len(filtered),
            )

        # ---------------------------------------------------------- Step 4: final top-K + metadata
        results: list[RagSearchResult] = []
        for candidate in filtered[:limit]:
            document = document_map[candidate.page_id]  # SUCCESS 集合保证存在，不会 KeyError
            results.append(
                RagSearchResult(
                    id=candidate.id,
                    page_id=candidate.page_id,
                    chunk_index=candidate.chunk_index,
                    chunk_text=candidate.chunk_text,
                    distance=candidate.distance,
                    document_id=document.id,  # document.id = Milvus.page_id（1:1）
                    filename=document.filename,
                    status=document.status,
                    created_at=document.created_at,
                    # Phase 3.1 Step 3：网页剪藏来源元数据（来源 documents 表，
                    # 经既有 document_map 关联填充，不新增 SQL / N+1）
                    title=document.title,
                    url=document.url,
                    source_type=document.source_type,
                )
            )
        return results
