"""
Milvus Repository Protocol（Phase 2.3 §3 正式落地）。

【严格阶段约束遵守】
- 仅定义接口（typing.Protocol），不含任何实现代码；
- 不 import pymilvus / MilvusClient（连接/创建 Collection 均在后续 Phase 2.4 Step 的 Impl/Initializer 中）；
- 不创建 Milvus Client、不连接 Milvus、不创建 Collection、不调用 upsert/search；
- 不依赖 FastAPI Depends / Service / API（分层解耦）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...models.milvus_dto import ChunkSearchResult, ChunkVector


@runtime_checkable
class MilvusRepository(Protocol):
    """
    Milvus page_chunks Collection 数据访问协议（与 Phase 2.3 §3.1 1:1 对齐）。

    实现约束：
    - 不感知 MySQL / Redis / 业务状态机；
    - 不吞异常：所有 pymilvus 异常统一包装为 core.exceptions.MilvusRepositoryError 族后向上抛出；
    - collection 名、连接参数从 core.config.Settings 注入，禁止在 Impl 内硬编码；
    - 每个方法 = 一次 Milvus 逻辑操作；不做 Service 级三步编排（query→upsert→delete 在 IngestService 里做）。
    """

    # ------------------------------------------------------------------ query
    def query_page_chunks(self, page_id: int, /) -> list[str]:
        """
        返回给定 page_id 下所有 chunk 的主键 id 字符串列表（顺序不承诺，调用方应使用 set()）。

        对应 Phase 2.2 §15 Step 4：query 旧 IDs，用于与 new_ids 做差集计算 stale_ids。
        空列表不是错误：首次 ingest 时 Milvus 可能没有该 page 的任何 chunk。
        """

    # ----------------------------------------------------------------- upsert
    def upsert_chunks(self, chunks: list[ChunkVector], /) -> None:
        """
        按 PK(id) 幂等写入 chunks；id 存在则覆盖，不存在则插入。

        对应 Phase 2.2 §15 Step 5（Milvus upsert 新 chunks）。
        【重要】本方法不负责 Chunk 切分、Embedding 生成、id 拼接——这些前置工作由 Service 层完成后，
        再构造 ChunkVector 列表传入。空 chunks 列表应立即返回，不向 Milvus 发请求。
        """

    # ----------------------------------------------------------------- delete
    def delete_chunks(self, ids: list[str], /) -> None:
        """
        按主键 id 列表精确删除 chunks。

        对应 Phase 2.2 §15 Step 7（delete stale by PK id）。
        红线：Impl 严禁按 page_id 条件删除；删除仅允许 `id in [...]` 精确按 PK。
        空 ids 列表应立即返回，不向 Milvus 发请求（避免误触发空表达式全删风险）。
        删除不存在的 id 视为成功，不抛异常。
        """

    # ----------------------------------------------------------------- search
    def search(
        self,
        vector: list[float],
        /,
        *,
        limit: int = 10,
        ef: int = 128,
        expr: str | None = None,
    ) -> list[ChunkSearchResult]:
        """
        向量 ANN 检索（Phase 2.2 §14.1；Phase 3.4 Step E 增 expr）。

        锁定参数（Impl 必须固定写常量，严禁允许调用方覆盖）：
          - metric_type = "COSINE"
          - output_fields = ["id", "page_id", "chunk_index", "chunk_text"]（严禁包含 embedding）

        Args:
            vector : 百炼 text-embedding-v3(dimensions=1024) 返回的 1024 维向量；
                     长度 != 1024 时 Impl 应抛 MilvusSchemaMismatchError（不可重试）。
            limit  : Milvus 初始候选召回数；默认 10（硬编码；RAG_TOP_K_CANDIDATES 未定义，
                     为未来预留配置，当前不读取），可覆盖。
            ef     : HNSW 查询候选池参数；默认 128（Phase 2.2 §10.2），可覆盖。
            expr   : 可选标量过滤表达式（Phase 3.4 Step E，用户隔离用）：
                     - 例如 "page_id == 1"（当前网页模式）或 "page_id in [1, 2]"（全库模式）；
                     - 默认 None = 全库 ANN 检索，行为与 Step E 前完全一致；
                     - 仅透传给 pymilvus client.search 的 expr 参数，不修改 Schema / index / metric。

        Returns:
            按 COSINE similarity 降序（最相似在前；由 Milvus 返回顺序保证）严格排序的结果列表，
            长度 ≤ limit；空列表 = 无结果。
            RagService 随后在应用层做 MySQL status post-filter（丢弃非 SUCCESS）并保留最终 top-5。
        """
