"""
MilvusInitializer：Page Chunks Collection 与 Index 幂等初始化器（Phase 2.4 Step 2）。

严格阶段约束遵守：
- 仅完成 Milvus Server → MilvusInitializer → Collection + Index + Load；
- 不实现 MilvusRepositoryImpl / query_page_chunks / upsert_chunks / delete_chunks / search；
- 不创建 Service / API / IngestService / RagService；
- 不引入 LangChain / LlamaIndex；
- 不修改文档；
- 不修改 Phase 2.3 Protocol / DTO 契约。

经验库引用：
- 153832「配置单一来源 + 禁止硬编码 collection 名 / host / port」：
  所有外部参数统一从 Settings（Milvus host/port/collection + embedding dim）读取；无任何
  `MilvusClient(uri="localhost:19530")` / `create_collection(collection_name="page_chunks")` 硬编码。
- 1362039「严格以 requirements.txt 锁定版本，禁止跨 minor 漂移」：
  仅允许与 `backend/requirements.txt` 锁定的 `pymilvus==2.4.15` API 兼容的调用能力
  （MilvusClient 高阶 API：create_schema / prepare_index_params / create_collection / load_collection）
  ；不使用 Milvus 2.5+/3.x 新增方法，避免未来版本冲突。
"""

from __future__ import annotations

import logging
from typing import Final

from pymilvus import MilvusClient, DataType

from ...core.config import Settings
from ...core.exceptions import MilvusOperationError

# Milvus PK / VARCHAR 长度（与 Phase 2.2 §6 / §7 锁定）
_ID_MAX_LENGTH: Final[int] = 64
# chunk_text UTF-8 字节上限（Phase 2.2 §9.3）
_CHUNK_TEXT_MAX_LENGTH: Final[int] = 4096

# HNSW 构建参数（Phase 2.2 §10.2 锁定）
_HNSW_M: Final[int] = 16
_HNSW_EF_CONSTRUCTION: Final[int] = 200
_HNSW_METRIC_TYPE: Final[str] = "COSINE"  # Phase 2.2 §11 锁定；不允许 IP/L2

# 常量：字段名（避免拼写错误；与 Phase 2.2 §6.1 顺序一致）
_FIELD_ID: Final[str] = "id"
_FIELD_PAGE_ID: Final[str] = "page_id"
_FIELD_CHUNK_INDEX: Final[str] = "chunk_index"
_FIELD_CHUNK_TEXT: Final[str] = "chunk_text"
_FIELD_EMBEDDING: Final[str] = "embedding"

# 获取当前模块 logger；项目后续若有统一 logger 配置会自动继承；当前阶段使用标准 logging
logger: logging.Logger = logging.getLogger(__name__)


class MilvusInitializer:
    """
    Milvus Collection + Index 幂等初始化器。

    初始化参数严格来自 Settings（单一配置源）：
        - milvus_host / milvus_port → 构建 MilvusClient 的 uri（无硬编码）
        - milvus_collection       → 待创建/幂等检查的 Collection 名称（禁止硬编码 "page_chunks"）
        - bailian_embedding_dimension → embedding FLOAT_VECTOR dim；默认 1024
    """

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: Pydantic Settings 单源实例（推荐通过 core.di.get_settings() 获取）。
        """
        self._settings: Settings = settings

    # ------------------------------------------------------------------ 对外入口
    def initialize(self) -> None:
        """
        创建 page_chunks Collection（含 Schema）、向量索引 HNSW(COSINE M=16 efConstruction=200)、
        标量索引 page_id INVERTED，并 load_collection。

        幂等性：
          - Collection 已存在：直接返回（不删除重建；Phase 2.2 §4 dim 一旦创建不可变，改配置必须
            走业务级重建流程，不允许在此自动覆盖）。
          - 调用多次等价于一次调用。

        Raises:
            MilvusOperationError: 任一 Milvus 调用失败时抛出，并保留原始异常链（不吞异常）。
        """
        collection_name = self._settings.milvus_collection
        embedding_dim = self._settings.bailian_embedding_dimension

        logger.info(
            "开始初始化 Milvus Collection：name=%s，embedding_dim=%s",
            collection_name,
            embedding_dim,
        )

        try:
            client_kwargs = self._build_client_kwargs()
            # 经验库 153832：连接与真实操作推迟到 initialize() 内部执行（不在 __init__ 打开连接，
            # 避免启动期硬依赖）。
            # 注意：pymilvus 2.4.15 MilvusClient 未实现 __enter__/__exit__，不支持 with 上下文管理器；
            # 显式构造 + try/finally close() 保证资源释放（close() 签名为 (self)）。
            client = MilvusClient(**client_kwargs)
            try:
                if client.has_collection(collection_name=collection_name):
                    logger.info(
                        "Milvus Collection 已存在：name=%s；跳过创建与索引构建。",
                        collection_name,
                    )
                    return

                schema = self._build_schema(client=client, embedding_dim=embedding_dim)
                index_params = client.prepare_index_params()
                self._add_vector_index(index_params=index_params)
                self._add_scalar_index_page_id(index_params=index_params)

                # enable_dynamic_fields=False 已在 schema 创建时显式关闭（Phase 2.2 不允许动态字段）
                client.create_collection(
                    collection_name=collection_name,
                    schema=schema,
                    index_params=index_params,
                )
                logger.info(
                    "Milvus Collection 创建成功：name=%s；已绑定 Schema + Index。",
                    collection_name,
                )
                logger.info(
                    "Milvus Index 创建完成：embedding(%s, M=%s, efConstruction=%s, metric=%s) + %s(INVERTED)。",
                    _FIELD_EMBEDDING,
                    _HNSW_M,
                    _HNSW_EF_CONSTRUCTION,
                    _HNSW_METRIC_TYPE,
                    _FIELD_PAGE_ID,
                )

                client.load_collection(collection_name=collection_name)
                logger.info("Milvus Collection load 成功：name=%s。", collection_name)
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001 — 按约束要求“不要吞异常 + 保留 __cause__”
            raise MilvusOperationError(
                f"MilvusInitializer.initialize 失败（collection={collection_name}, "
                f"host={self._settings.milvus_host}:{self._settings.milvus_port}）：{exc}"
            ) from exc

    # ------------------------------------------------------------------ 内部辅助
    def _build_client_kwargs(self) -> dict[str, str]:
        """
        MilvusClient 构造参数（单一配置源；零硬编码）。

        pymilvus 2.4.x MilvusClient 的推荐构造方式：
          MilvusClient(uri="http://<host>:<port>")   # 内部使用 gRPC
        故此处直接拼接为 f"http://host:port" 单字符串；避免显式传 user/pass（当前未启用鉴权）。
        """
        uri = f"http://{self._settings.milvus_host}:{self._settings.milvus_port}"
        return {"uri": uri}

    @staticmethod
    def _build_schema(*, client: MilvusClient, embedding_dim: int):
        """
        构建 Milvus Schema（严格 Phase 2.2 §6/§7）。

        字段顺序（与 Phase 2.2 §6.1 完全一致）：
          1) id          VARCHAR(64)   PRIMARY KEY   auto_id=False
          2) page_id     INT64         non-null
          3) chunk_index INT64         non-null
          4) chunk_text  VARCHAR(4096) non-null       UTF-8 字节上限
          5) embedding   FLOAT_VECTOR(dim)            dim=Settings.bailian_embedding_dimension（默认 1024）

        显式关闭 enable_dynamic_fields（Phase 2.2 禁止动态字段，避免 Impl 写入 DTO 外字段。）
        """
        schema = client.create_schema(auto_id=False, enable_dynamic_fields=False)

        schema.add_field(
            field_name=_FIELD_ID,
            datatype=DataType.VARCHAR,
            max_length=_ID_MAX_LENGTH,
            is_primary=True,
        )
        schema.add_field(field_name=_FIELD_PAGE_ID, datatype=DataType.INT64)
        schema.add_field(field_name=_FIELD_CHUNK_INDEX, datatype=DataType.INT64)
        schema.add_field(
            field_name=_FIELD_CHUNK_TEXT,
            datatype=DataType.VARCHAR,
            max_length=_CHUNK_TEXT_MAX_LENGTH,
        )
        schema.add_field(
            field_name=_FIELD_EMBEDDING,
            datatype=DataType.FLOAT_VECTOR,
            dim=embedding_dim,
        )
        return schema

    @staticmethod
    def _add_vector_index(*, index_params) -> None:
        """
        向量索引（Phase 2.2 §10 HNSW + §11 COSINE）：M=16 / efConstruction=200 / metric=COSINE。
        字段：embedding。
        """
        index_params.add_index(
            field_name=_FIELD_EMBEDDING,
            index_type="HNSW",
            metric_type=_HNSW_METRIC_TYPE,
            params={"M": _HNSW_M, "efConstruction": _HNSW_EF_CONSTRUCTION},
        )

    @staticmethod
    def _add_scalar_index_page_id(*, index_params) -> None:
        """
        标量索引（Phase 2.2 §12：page_id → INVERTED）；用于 re-ingest Step 4 按 page_id 旧 IDs 查询。
        """
        index_params.add_index(field_name=_FIELD_PAGE_ID, index_type="INVERTED")
