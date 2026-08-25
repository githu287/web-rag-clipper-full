"""
全局应用配置：Pydantic v2 Settings 单源。

设计原则（经验库 153832「配置单一来源」规则应用）：
1) 所有外部配置只从一个类读取，避免「改了 .env.example / 代码常量 / 工厂默认」三套来源造成漂移。
2) Milvus 与百炼 Embedding 维度等关键值均从该类取，Impl 层禁止硬编码。
3) 读取顺序：显式环境变量 > .env 文件 > Settings 类声明的安全默认值（如 BAILIAN_EMBEDDING_DIMENSION=1024）。

本阶段（Phase 2.4 Step 1）仅暴露 Milvus / Embedding 维度等基础契约字段；后续 Service/API 新增字段可在此扩展。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Milvus 相关默认值（与 .env.example / Phase 2.2 §1/§5 对齐）
_DEFAULT_MILVUS_HOST: Final[str] = "localhost"
_DEFAULT_MILVUS_PORT: Final[int] = 19530
_DEFAULT_MILVUS_COLLECTION: Final[str] = "page_chunks"

# Embedding 维度默认值（Phase 2.2 §4 最终锁定：1024；Phase 2.2 §19.1 阻塞项 #1；不得改为 0/None）
_DEFAULT_BAILIAN_EMBEDDING_DIMENSION: Final[int] = 1024

# 百炼 OpenAI 兼容 API 默认值（与 .env.example L30-36 对齐）
_DEFAULT_BAILIAN_BASE_URL: Final[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_BAILIAN_EMBEDDING_MODEL: Final[str] = "text-embedding-v3"

# 百炼 LLM 模型默认值（Phase 3.3 Step 3 新增；与 .env.example L37 BAILIAN_LLM_MODEL=qwen-plus 对齐）
_DEFAULT_BAILIAN_LLM_MODEL: Final[str] = "qwen-plus"

# 百炼 text-embedding-v3 单次请求最大行数硬限制为 10（.env.example L42 注释明确）
_DEFAULT_EMBEDDING_BATCH_SIZE: Final[int] = 10
_EMBEDDING_BATCH_SIZE_MAX: Final[int] = 10

# MySQL 默认值（与 .env.example 的 MYSQL_* 对齐；Phase 2.9 Step 1 新增）
_DEFAULT_MYSQL_HOST: Final[str] = "localhost"
_DEFAULT_MYSQL_PORT: Final[int] = 3306
_DEFAULT_MYSQL_DATABASE: Final[str] = "rag_clipper"

# 文件上传 / 解析 / 切分默认值（与 .env.example 的 CHUNK_* / MAX_PAGE_CONTENT_BYTES 对齐；Phase 2.10 Step 2 新增）
_DEFAULT_UPLOAD_DIR: Final[str] = "uploads"
_DEFAULT_CHUNK_SIZE: Final[int] = 700
_DEFAULT_CHUNK_OVERLAP: Final[int] = 100
_DEFAULT_MAX_PAGE_CONTENT_BYTES: Final[int] = 2097152  # 2 MiB


class Settings(BaseSettings):
    """
    应用运行时配置单源。

    字段名与 .env.example 环境变量一一对应：
    - MILVUS_HOST / MILVUS_PORT / MILVUS_COLLECTION
    - BAILIAN_API_KEY / BAILIAN_BASE_URL / BAILIAN_EMBEDDING_MODEL
    - BAILIAN_EMBEDDING_DIMENSION / EMBEDDING_BATCH_SIZE
    - MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
    """

    # Milvus 连接与集合
    milvus_host: str = Field(
        default=_DEFAULT_MILVUS_HOST,
        description="Milvus gRPC 连接主机（docker-compose 默认 standalone 本地）",
    )
    milvus_port: int = Field(
        default=_DEFAULT_MILVUS_PORT,
        ge=1,
        le=65535,
        description="Milvus gRPC 端口（docker-compose 默认 19530）",
    )
    milvus_collection: str = Field(
        default=_DEFAULT_MILVUS_COLLECTION,
        min_length=1,
        description="Milvus Collection 名称（Phase 2.2 §5 锁定 page_chunks；禁止 Impl 硬编码）",
    )

    # 百炼 Embedding 维度（与 Milvus embedding.dim=1024 严格一致；改值需重建 Collection 并重 ingesting）
    bailian_embedding_dimension: int = Field(
        default=_DEFAULT_BAILIAN_EMBEDDING_DIMENSION,
        ge=1,
        description="百炼 text-embedding-v3 dimensions 参数；必须与 Milvus Collection FLOAT_VECTOR.dim 完全一致",
    )

    # 百炼 API 鉴权与端点（Phase 2.5 Step 1-A 新增；供后续 EmbeddingClient 使用）
    bailian_api_key: str = Field(
        default="",
        description="阿里云百炼 API Key（DASHSCOPE_API_KEY）；必须通过环境变量注入，禁止代码硬编码",
    )
    bailian_base_url: str = Field(
        default=_DEFAULT_BAILIAN_BASE_URL,
        description="百炼 OpenAI 兼容模式 base_url；默认 dashscope.aliyuncs.com/compatible-mode/v1",
    )
    bailian_embedding_model: str = Field(
        default=_DEFAULT_BAILIAN_EMBEDDING_MODEL,
        min_length=1,
        description="百炼 Embedding 模型名；默认 text-embedding-v3",
    )
    bailian_llm_model: str = Field(
        default=_DEFAULT_BAILIAN_LLM_MODEL,
        min_length=1,
        description="百炼 LLM 模型名（Chat Completion）；默认 qwen-plus",
    )

    # ---- 认证体系（Phase 3.4 Step D 新增）----
    # AES-256-GCM 主密钥：用于加密 / 解密用户自己的百炼 API Key
    # （users.api_key_ciphertext / nonce）。必须通过环境变量注入（32 字节），
    # 禁止代码硬编码；与 Security.encrypt_api_key 的 master_key 语义对齐。
    app_master_key: str = Field(
        default="",
        description="AES-256-GCM 主密钥（32 字节）；AuthService 用于加密/解密用户百炼 API Key",
    )

    # Embedding 分批大小（百炼 text-embedding-v3 单次请求最大行数硬限制为 10）
    embedding_batch_size: int = Field(
        default=_DEFAULT_EMBEDDING_BATCH_SIZE,
        ge=1,
        le=_EMBEDDING_BATCH_SIZE_MAX,
        description="百炼 Embedding 单次请求最大文本行数；硬上限 10，禁止超过",
    )

    # MySQL 连接（Phase 2.9 Step 1 新增；.env MYSQL_* 一一对应；密码支持特殊字符，db.py 用 quote_plus 转义）
    mysql_host: str = Field(default=_DEFAULT_MYSQL_HOST, description="MySQL 主机")
    mysql_port: int = Field(
        default=_DEFAULT_MYSQL_PORT,
        ge=1,
        le=65535,
        description="MySQL 端口",
    )
    mysql_user: str = Field(default="rag_user", description="MySQL 用户名")
    mysql_password: str = Field(
        default="",
        description="MySQL 密码（支持特殊字符，db.py 用 quote_plus 转义后再拼入 URL）",
    )
    mysql_database: str = Field(default=_DEFAULT_MYSQL_DATABASE, description="MySQL 数据库名")

    # ---- 文件上传 / 解析 / 切分（Phase 2.10 Step 2 新增）----
    upload_dir: str = Field(
        default=_DEFAULT_UPLOAD_DIR,
        min_length=1,
        description="原始文件本地存储根目录（相对项目根或绝对路径）；LocalFileStorage 使用",
    )
    chunk_size: int = Field(
        default=_DEFAULT_CHUNK_SIZE,
        ge=1,
        description="Chunker 单块最大字符数",
    )
    chunk_overlap: int = Field(
        default=_DEFAULT_CHUNK_OVERLAP,
        ge=0,
        description="相邻 chunk 重叠字符数（必须小于 chunk_size，见 model_validator）",
    )
    max_page_content_bytes: int = Field(
        default=_DEFAULT_MAX_PAGE_CONTENT_BYTES,
        ge=1,
        description="单文件最大字节数；超过该上限的文本应在进入 Chunker 前由上层拒绝",
    )

    @model_validator(mode="after")
    def _validate_chunker_config(self) -> "Settings":
        """Chunker 参数交叉约束：chunk_overlap 必须严格小于 chunk_size。"""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"chunk_overlap({self.chunk_overlap}) 必须小于 chunk_size({self.chunk_size})"
            )
        return self

    # 读取 .env 文件兜底；当容器内环境变量已显式注入时，环境变量优先级高于 .env（Pydantic-settings 默认行为）
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_default_settings() -> Settings:
    """
    Settings 零参数工厂：供依赖注入与无 DI 场景共享。

    注意：
    - 使用 LRU 缓存保证进程内单例（避免重复解析 .env / 重复实例化）。
    - 若未来需要在测试中覆盖配置，请优先用 monkeypatch 环境变量，而不是直接改该函数默认值。
    """  # noqa: D401
    return Settings()


# 别名：供 core.db 等基础设施层共享同一份 lru_cache 单例。
# 与 core.di.get_settings() 包装器语义对齐；不在 db.py 内自引用，避免循环导入。
get_settings = get_default_settings
