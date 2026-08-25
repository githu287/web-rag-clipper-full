"""
PyMilvusRepositoryImpl：MilvusRepository Protocol 的 pymilvus 2.4.15 实现（Phase 2.4 Step 3）。

严格阶段约束遵守：
- 仅实现 MilvusRepository 协议定义的 4 个方法；
- 不修改 Protocol / DTO / Schema / MilvusInitializer；
- 不创建 Service / API / IngestService / RagService；
- 不引入 LangChain / LlamaIndex；
- 不调用百炼 Embedding API（向量由 Service 层传入）。

经验库 153832 规则应用：
1) 连接参数/集合名 **单一配置源** 全部来自 Settings（milvus_host / milvus_port / milvus_collection /
   bailian_embedding_dimension），无任何 `MilvusClient(uri="localhost:19530")` 或
   `collection_name="page_chunks"` 硬编码；
2) **真实 MilvusClient 连接延迟到每个方法内部建立**（不在 __init__ 打开连接），避免启动期硬依赖；
   每个方法内部显式构造 MilvusClient 并用 try/finally close() 保证资源释放
   （pymilvus 2.4.15 MilvusClient 未实现 __enter__/__exit__，不支持 with）；
3) DTO 构造已在 Service 层完成，本 impl 只在 Milvus 写入前做「契约一致防御」，任何 Milvus 错误不吞，
   统一包装为 MilvusRepositoryError 族并保留异常链 __cause__。

经验库 610470（Milvus DataNotMatchException 防御）规则应用：
- upsert_chunks 使用 Pydantic ChunkVector.model_dump() 输出 dict 列表，字段与 Schema 1:1；不手动拼字段。
- search 方法在调用 Milvus 前强校验 len(vector) == bailian_embedding_dimension，若不一致抛出
  MilvusSchemaMismatchError（不可重试），避免在 Milvus 端才暴露维度错，导致 DataNotMatchException
  日志难以定位。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Final

from pymilvus import MilvusClient

from ...core.config import Settings
from ...core.exceptions import (
    MilvusConnectionError,
    MilvusOperationError,
    MilvusSchemaMismatchError,
)
from ...models.milvus_dto import ChunkSearchResult, ChunkVector
from .protocol import MilvusRepository

# Milvus 结果中的标准键（pymilvus 2.4 MilvusClient.search 返回 list[dict]，每条包含 entity/distance）
_MILVUS_RESULT_KEY_ENTITY: Final[str] = "entity"
_MILVUS_RESULT_KEY_DISTANCE: Final[str] = "distance"

# Search：固定 output_fields（Phase 2.2 §13.1；严禁包含 embedding）
_SEARCH_OUTPUT_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "page_id",
    "chunk_index",
    "chunk_text",
)

# Search：固定 metric_type（Phase 2.2 §11 锁定 COSINE；不允许 IP/L2）
_SEARCH_METRIC_TYPE: Final[str] = "COSINE"

# pymilvus 抛出的异常中如果包含以下子串之一，则视为连接/网络类错误 → MilvusConnectionError
# （基于 pymilvus 2.4.x 常见 gRPC/MilvusException 的错误文案做启发式匹配；无法识别时统一落 MilvusOperationError）
_CONNECTION_ERROR_MARKERS: Final[tuple[str, ...]] = (
    "connection",
    "connect",
    "unavailable",
    "deadline_exceeded",
    "timeout",
    "timed out",
    "transport",
    "dns",
    "refused",
    "reset",
    "rpc",
    "etcd",
    "proxy",
)

logger: logging.Logger = logging.getLogger(__name__)


def _is_connection_error(exc: BaseException) -> bool:
    """基于异常消息字符串与异常类型名的启发式连接错误判定（内部辅助）。"""
    candidates: Iterable[str] = (
        str(exc),
        type(exc).__name__,
        type(exc).__module__,
    )
    joined = " ".join(candidates).lower()
    return any(marker in joined for marker in _CONNECTION_ERROR_MARKERS)


def _wrap_milvus_error(message_prefix: str, exc: BaseException) -> MilvusOperationError:
    """
    将 pymilvus 抛出的异常包装为 MilvusRepositoryError 族，并保留 __cause__。

    选择规则：
      - 启发式识别到连接/超时/transport 错误 → MilvusConnectionError（可由 Service 层任务内重试）
      - 其他错误 → MilvusOperationError（按场景决定重试或转 FAILED）
    """
    err_cls = MilvusConnectionError if _is_connection_error(exc) else MilvusOperationError
    # 注意：__cause__ 异常链由 4 个调用点的 `raise _wrap_milvus_error(...) from exc` 设置；
    # 函数本身仅负责按启发式分类构造异常对象（工厂函数语义，不在此处 raise）。
    return err_cls(f"{message_prefix}：{exc}")


class PyMilvusRepositoryImpl(MilvusRepository):
    """
    MilvusRepository 的 pymilvus 2.4.15 实现。

    注入：
        settings：唯一配置源；
            - milvus_host / milvus_port → 构造 MilvusClient(uri=f"http://host:port")
            - milvus_collection          → 本实例管理的 Collection 名称
            - bailian_embedding_dimension → search 向量维度 / upsert 二次防御用
    """

    def __init__(self, settings: Settings) -> None:
        self._settings: Settings = settings
        self._uri: str = f"http://{settings.milvus_host}:{settings.milvus_port}"
        self._collection_name: str = settings.milvus_collection
        self._embedding_dimension: int = settings.bailian_embedding_dimension

    # ------------------------------------------------------------------ 连接工厂
    def _make_client(self) -> MilvusClient:
        """
        新建 MilvusClient 实例（经验库 153832：延迟到每个方法内部使用）。
        调用方必须保证用完后关闭：使用 `client = self._make_client()` + `try/finally: client.close()`
        （pymilvus 2.4.15 MilvusClient 未实现 __enter__/__exit__，不支持 with）。
        """
        return MilvusClient(uri=self._uri)

    # ================================================================ 4 个方法
    # ---------------------------------------------------------------- query
    def query_page_chunks(self, page_id: int, /) -> list[str]:
        """
        按 page_id 查询所有 chunk 的 PK id 列表（Phase 2.3 Protocol 方法 1）。

        - 过滤：filter = f"page_id == {page_id}"（使用 Milvus 标量 INVERTED 索引，见 Phase 2.2 §12）
        - output_fields = ["id"]（窄投影，避免返回 chunk_text/embedding）
        - 空结果：返回 []，不抛异常。
        """
        msg_prefix = (
            f"PyMilvusRepositoryImpl.query_page_chunks 失败（collection={self._collection_name},"
            f" page_id={page_id})"
        )
        expr = f"page_id == {int(page_id)}"
        try:
            client = self._make_client()
            try:
                rows: list[dict] = client.query(
                    collection_name=self._collection_name,
                    filter=expr,
                    output_fields=["id"],
                )
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 — 统一包装 + 保留 __cause__
            raise _wrap_milvus_error(msg_prefix, exc) from exc

        # 防御：Milvus 返回结构可能缺 id；但我们显式 output_fields=["id"]，缺字段视为操作错误。
        ids: list[str] = []
        for row in rows:
            chunk_id = row.get("id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise MilvusOperationError(
                    f"{msg_prefix}：Milvus 返回行缺少字符串 id：row={row!r}"
                )
            ids.append(chunk_id)
        return ids

    # ---------------------------------------------------------------- upsert
    def upsert_chunks(self, chunks: list[ChunkVector], /) -> None:
        """
        按 PK 幂等 upsert 一个 ChunkVector 列表（Phase 2.3 Protocol 方法 2）。

        - 空列表：直接 return，不向 Milvus 发请求。
        - 幂等：Milvus upsert 语义 = 「同 PK 覆盖 / 新 PK 插入」→ N 次重复调用等价于 1 次。
        - 禁止：不调用任何 Embedding API；不负责 DTO 构造（Service 层传入）。
        - 经验库 610470：通过 Pydantic.model_dump() 保证字段集与 Milvus Schema 完全一致，
          避免 DataNotMatchException（字段缺 / 字段多余）。
        """
        if not chunks:
            return

        msg_prefix = (
            f"PyMilvusRepositoryImpl.upsert_chunks 失败（collection={self._collection_name},"
            f" count={len(chunks)})"
        )
        try:
            data: list[dict] = [c.model_dump() for c in chunks]
        except Exception as exc:  # Pydantic 序列化/校验失败；视为契约错误（不可重试）
            raise MilvusSchemaMismatchError(
                f"{msg_prefix}：ChunkVector 序列化/字段校验失败：{exc}"
            ) from exc

        try:
            client = self._make_client()
            try:
                client.upsert(collection_name=self._collection_name, data=data)
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            raise _wrap_milvus_error(msg_prefix, exc) from exc

    # ---------------------------------------------------------------- delete
    def delete_chunks(self, ids: list[str], /) -> None:
        """
        按 PK 精确删除 chunks（Phase 2.3 Protocol 方法 3）。

        红线（Phase 2.3 §5.3 强制）：
          - 仅使用 ids = [...] 精确按主键列表删除（PK 值列表直传 MilvusClient.delete(ids=...)）；
          - 严禁 `page_id == x` 条件删除（会误删其他并发 re-ingest 的新 chunk）。
        空 ids：立即 return，不触发 Milvus 请求（避免误触发空表达式）。
        不存在的 PK：Milvus 视为成功（幂等）。
        """
        if not ids:
            return

        msg_prefix = (
            f"PyMilvusRepositoryImpl.delete_chunks 失败（collection={self._collection_name},"
            f" ids_count={len(ids)})"
        )
        # 经验库 Phase 2.8 Step 3.1：MilvusClient.delete(ids=...) 接受 PK 值列表（list/str/int），
        # filter=... 才接受过滤表达式；二者互斥。故直接传 PK 列表，不构造 `id in [...]` 表达式
        # （原 ids=expr 会把表达式字符串当作单个 PK，导致 `id in ['id in [...]']` 畸形表达式 → code=1100）。
        try:
            client = self._make_client()
            try:
                client.delete(collection_name=self._collection_name, ids=ids)
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            raise _wrap_milvus_error(msg_prefix, exc) from exc

    # ---------------------------------------------------------------- search
    def search(
        self,
        vector: list[float],
        /,
        *,
        limit: int = 10,
        ef: int = 128,
    ) -> list[ChunkSearchResult]:
        """
        向量 ANN 检索（Phase 2.3 Protocol 方法 4）。

        锁定常量：
          - metric_type="COSINE"（Phase 2.2 §11）
          - output_fields = [id, page_id, chunk_index, chunk_text]（**严禁包含 embedding**，Phase 2.2 §13.1）

        校验：
          - 输入 vector 长度必须 == settings.bailian_embedding_dimension（默认 1024），
            否则立即抛 MilvusSchemaMismatchError（不可重试；Milvus 端会写入错误维度或报维度错，
            在应用层防御避免 DataNotMatchException）。
        """
        msg_prefix = (
            f"PyMilvusRepositoryImpl.search 失败（collection={self._collection_name},"
            f" limit={limit}, ef={ef})"
        )
        # Phase 2.3 校验要求：vector 维度必须等于 settings.bailian_embedding_dimension
        if len(vector) != self._embedding_dimension:
            raise MilvusSchemaMismatchError(
                f"{msg_prefix}：vector 维度 {len(vector)} 与配置"
                f" bailian_embedding_dimension={self._embedding_dimension} 不一致"
                "（此为不可重试契约错误；如需改维度请重建 Collection 并重 ingesting）。"
            )

        try:
            client = self._make_client()
            try:
                results: list[dict] = client.search(
                    collection_name=self._collection_name,
                    data=[list(vector)],  # MilvusClient.search 要求 data 为「向量列表的列表」
                    limit=limit,
                    output_fields=list(_SEARCH_OUTPUT_FIELDS),
                    search_params={
                        "metric_type": _SEARCH_METRIC_TYPE,
                        "params": {"ef": ef},
                    },
                )
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            raise _wrap_milvus_error(msg_prefix, exc) from exc

        # pymilvus 2.4 MilvusClient.search 返回结构：外层为 N 个 query vectors 结果列表；
        # 我们 data=[vector]，所以取 results[0] 作为当前向量的命中列表。
        hits: list[dict] = results[0] if results else []
        out: list[ChunkSearchResult] = []
        for hit in hits:
            entity = hit.get(_MILVUS_RESULT_KEY_ENTITY)
            distance = hit.get(_MILVUS_RESULT_KEY_DISTANCE)
            if not isinstance(entity, dict) or distance is None:
                raise MilvusOperationError(
                    f"{msg_prefix}：Milvus search 返回行缺少 entity/distance：hit={hit!r}"
                )
            try:
                result = ChunkSearchResult(
                    id=str(entity["id"]),
                    page_id=int(entity["page_id"]),
                    chunk_index=int(entity["chunk_index"]),
                    chunk_text=str(entity["chunk_text"]),
                    distance=float(distance),
                )
            except Exception as exc:  # noqa: BLE001 — 字段缺失/类型不符属于 Schema 返回契约不匹配
                raise MilvusSchemaMismatchError(
                    f"{msg_prefix}：构造 ChunkSearchResult 失败（output_fields 不完整或维度错）："
                    f"entity={entity!r}, distance={distance!r}：{exc}"
                ) from exc
            out.append(result)
        return out
