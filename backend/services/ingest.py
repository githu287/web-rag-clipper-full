"""
IngestService：页面 chunk 入库编排（Phase 2.6 Step 2）。

职责：
    将已切分的 chunk 文本列表编排写入 Milvus，严格执行 Phase 2.2 §15 re-ingest 三步流程：
      1) query old_ids —— 查询该 page 当前已存在的 chunk PK id 列表
      2) upsert new_chunks —— embedding + 构造 ChunkVector + Milvus upsert
      3) delete stale_ids —— 差集计算后按 PK 精确删除过期 chunk

依赖注入：
    - EmbeddingClient：文本 → embedding 向量
    - MilvusRepository（Protocol）：Milvus 数据访问

严格不依赖：
    - pymilvus / MilvusClient（由 Repository Impl 内部隔离）
    - openai / OpenAI（由 EmbeddingClient 内部隔离）
    - FastAPI / Request（API 层职责）

幂等性：
    相同 page_id + 相同 chunks 多次调用 → 结果一致（upsert 按 PK 覆盖 + delete 差集）。

异步说明：
    本 Service 使用 async def（为未来异步 Repository/EmbeddingClient 预留接口契约），
    但当前 Repository 与 EmbeddingClient 均为同步实现，方法内部直接同步调用（不使用
    asyncio.to_thread()，不引入异步 SDK，不重构 Repository）。
"""

from __future__ import annotations

import logging

from ..clients.embedding import EmbeddingClient
from ..models.milvus_dto import ChunkVector
from ..repositories.milvus import MilvusRepository

logger: logging.Logger = logging.getLogger(__name__)


class IngestService:
    """
    页面 chunk 入库编排服务。

    构造依赖通过 DI 注入（未来 core/di.py 新增 get_ingest_service() 工厂）：
        embedding_client : EmbeddingClient       — 文本嵌入
        milvus_repository: MilvusRepository       — Milvus 数据访问（Protocol 类型）
    """

    def __init__(
        self,
        embedding_client: EmbeddingClient,
        milvus_repository: MilvusRepository,
    ) -> None:
        """
        注入 EmbeddingClient + MilvusRepository。

        Args:
            embedding_client: 百炼 Embedding 客户端实例（同步 embed 方法）。
            milvus_repository: Milvus Repository 实例（Protocol 类型，不依赖具体 Impl）。
        """
        self._embedding: EmbeddingClient = embedding_client
        self._repo: MilvusRepository = milvus_repository

    # ------------------------------------------------------------------ 对外入口
    async def ingest_page(
        self,
        page_id: int,
        chunks: list[str],
        user_id: int,
        api_key: str,
    ) -> None:
        """
        将一个页面的 chunk 列表入库 Milvus（re-ingest 三步流程）。

        流程（Phase 2.2 §15）：
          Step 1: query old_ids = repo.query_page_chunks(page_id)
          Step 2: 若 chunks 非空 → embedding → 构造 ChunkVector 列表 → repo.upsert_chunks(new_vectors)
          Step 3: stale_ids = old_ids - new_ids → repo.delete_chunks(stale_ids)

        幂等性：
          - upsert 按 PK(id) 覆盖，重复 ingest 相同 chunks 结果一致；
          - delete 仅删除差集（old 有但 new 无的 id），不会误删新写入的 chunk；
          - 若 chunks 为空：跳过 embedding/upsert，仅删除所有旧 chunk（等于清空该 page）。

        Args:
            page_id: 页面 ID（对应 Document.id，当前 document.id = Milvus.page_id 1:1 映射；非负整数）。
            chunks: 已切分的 chunk 文本列表（每条非空字符串）。
            user_id: 当前登录用户 ID（Phase 3.4 Step F6：来自 current_user.id，匿名 ingest 禁止）。
            api_key: 当前用户的百炼 API Key（Phase 3.4 Step F6：必填，由 AuthService
                解密后经上层 Service 传入；严禁回退 settings.bailian_api_key）。

        Raises:
            EmbeddingClientError: 百炼 Embedding 调用失败（向上传播，不吞异常）。
            MilvusRepositoryError: Milvus 操作失败（向上传播，不吞异常）。
            ValueError: chunks 含空字符串（EmbeddingClient.embed 内部校验抛出）。
        """
        # ---------------------------------------------------------- Step 1: 查询旧数据
        old_ids: list[str] = self._repo.query_page_chunks(page_id)
        old_ids_set: set[str] = set(old_ids)
        logger.info(
            "IngestService.ingest_page: page_id=%s, user_id=%s, 查询到旧 chunk 数=%d, 新 chunk 数=%d",
            page_id,
            user_id,
            len(old_ids_set),
            len(chunks),
        )

        # ---------------------------------------------------------- Step 2: 生成 + 写入新数据
        if not chunks:
            # chunks 为空：跳过 embedding/upsert，直接进入 Step 3 删除全部旧 chunk
            new_ids_set: set[str] = set()
        else:
            # 调用 EmbeddingClient 生成向量（同步调用，不 await）
            # Phase 3.4 Step F6：必须使用当前用户自己的 API Key（调用方 decrypt 后传入）
            vectors: list[list[float]] = self._embedding.embed(
                chunks, api_key=api_key
            )

            # 构造 ChunkVector 列表（确定性 id = f"{page_id}_{chunk_index}"）
            new_vectors: list[ChunkVector] = []
            for chunk_index, (chunk_text, embedding) in enumerate(zip(chunks, vectors, strict=True)):
                chunk_vector = ChunkVector(
                    id=f"{page_id}_{chunk_index}",
                    page_id=page_id,
                    chunk_index=chunk_index,
                    chunk_text=chunk_text,
                    embedding=embedding,
                )
                new_vectors.append(chunk_vector)

            new_ids_set = {cv.id for cv in new_vectors}

            # 写入 Milvus（同步调用，不 await）
            self._repo.upsert_chunks(new_vectors)
            logger.info(
                "IngestService.ingest_page: page_id=%s, upsert 新 chunk 数=%d",
                page_id,
                len(new_vectors),
            )

        # ---------------------------------------------------------- Step 3: 删除过期数据
        stale_ids: list[str] = sorted(old_ids_set - new_ids_set)
        if stale_ids:
            self._repo.delete_chunks(stale_ids)
            logger.info(
                "IngestService.ingest_page: page_id=%s, 删除过期 chunk 数=%d",
                page_id,
                len(stale_ids),
            )
        else:
            logger.info(
                "IngestService.ingest_page: page_id=%s, 无过期 chunk 需删除",
                page_id,
            )
