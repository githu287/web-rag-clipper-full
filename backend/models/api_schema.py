"""
API Schema 定义（Phase 2.7 Step 2；Phase 2.13 Step 2 增 RAG Document Metadata）。

本文件定义 HTTP 接口的请求 / 响应 Pydantic Schema，与内部 DTO（models/milvus_dto.py）
严格分离：
- 内部 DTO（ChunkVector / ChunkSearchResult）：Repository / Service 层数据契约；
- API Schema（IngestRequest / IngestResponse / RagSearchRequest / RagSearchResult /
  RagSearchResponse）：HTTP 接口边界契约，对外暴露的字段集。

复用规则（Phase 2.13 Step 2）：
- RagSearchResponse.results 元素类型为 RagSearchResult：继承 models.milvus_dto.ChunkSearchResult
  的 5 个检索字段（id / page_id / chunk_index / chunk_text / distance），扩展 Document
  metadata 字段（document_id / filename / status / created_at）。
- 严禁在 API Schema 中重新声明 ChunkSearchResult 字段（继承复用，避免契约漂移）；
  严禁直接修改 ChunkSearchResult（Milvus 内部 DTO 职责不与 Document 元数据耦合）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .milvus_dto import ChunkSearchResult


# ---------------------------------------------------------------------------
# Ingest 接口 Schema
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    """
    POST /ingest/page 请求体。

    字段：
        page_id : 页面 ID（必须 > 0；对应 Document.id，当前 document.id = Milvus.page_id 1:1）。
        chunks  : 已切分的 chunk 文本列表（必须非空；每条非空字符串）。
    """

    model_config = ConfigDict(extra="forbid")

    page_id: int = Field(
        ...,
        gt=0,
        description="页面 ID（对应 Document.id；必须 > 0）",
    )
    chunks: list[str] = Field(
        ...,
        min_length=1,
        description="已切分的 chunk 文本列表（必须非空）",
    )


class IngestResponse(BaseModel):
    """
    POST /ingest/page 响应体。

    字段：
        success : 是否成功（默认 True）。
        message : 描述信息（默认 "success"）。
    """

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(default=True, description="是否成功")
    message: str = Field(default="success", description="描述信息")


# ---------------------------------------------------------------------------
# RAG 接口 Schema
# ---------------------------------------------------------------------------


class RagSearchRequest(BaseModel):
    """
    POST /rag/search 请求体。

    字段：
        query       : 用户查询文本（必须非空字符串）。
        limit       : 最终返回结果数上限；默认 5；范围 1 <= limit <= 20。
        document_id : 可选；限定检索范围（当前网页模式，Phase 3.4 Step E）；
                      None = 全部知识库模式。ownership 由后端 plugin_id 校验，
                      不允许客户端通过 document_id 访问其它 Plugin 的文档。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        description="用户查询文本（必须非空）",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="最终返回结果数上限；范围 1-20；默认 5",
    )
    document_id: int | None = Field(
        default=None,
        gt=0,
        description="限定检索范围（当前网页模式）；None=全部知识库模式",
    )


class RagSearchResult(ChunkSearchResult):
    """
    RAG 检索结果 item（API 边界 DTO，Phase 2.13 Step 2；Phase 3.1 Step 3 扩展）。

    继承 ChunkSearchResult 的 5 个检索字段（id / page_id / chunk_index / chunk_text / distance），
    扩展 Document 元数据字段（document_id / filename / status / created_at /
    title / url / source_type），共 12 个字段。

    关联约定：
        document_id = page_id（document.id = Milvus.page_id 1:1，数值相同）。
        filename / status / created_at / title / url / source_type 来自 MySQL
        documents 表（RagService 经 get_documents_by_ids 批量反查后按
        document_map[id] 关联填充，不新增 SQL / N+1）。

    兼容性：
        新增字段均为 optional，旧客户端解析原有 5 字段不受影响。
        不返回 file_path（后端存储路径，不在 API 边界暴露）。
    """

    document_id: int | None = Field(
        default=None,
        ge=0,
        description="Document 主键 ID（= page_id，1:1；仅 SUCCESS 文档会出现）",
    )
    filename: str | None = Field(
        default=None,
        max_length=255,
        description="Document 文件名（来源 documents.filename）",
    )
    status: str | None = Field(
        default=None,
        description="Document 状态（当前返回 SUCCESS）",
    )
    created_at: datetime | None = Field(
        default=None,
        description="Document 创建时间（来源 documents.created_at，ISO 8601）",
    )
    title: str | None = Field(
        default=None,
        max_length=512,
        description="网页剪藏标题（来源 documents.title；上传文档为 None）",
    )
    url: str | None = Field(
        default=None,
        max_length=2048,
        description="网页剪藏来源 URL（来源 documents.url；上传文档为 None）",
    )
    source_type: str | None = Field(
        default=None,
        max_length=32,
        description="来源类型（upload / webpage，来源 documents.source_type）",
    )


class RagSearchResponse(BaseModel):
    """
    POST /rag/search 响应体。

    字段：
        results : 检索结果列表，元素类型为 RagSearchResult（API 边界 DTO）：
                  继承 ChunkSearchResult 的 id / page_id / chunk_index / chunk_text / distance，
                  扩展 Document 元数据 document_id / filename / status / created_at。
                  document_id = page_id（1:1）；不含 embedding；不含 file_path。
    """

    model_config = ConfigDict(extra="forbid")

    results: list[RagSearchResult] = Field(
        default_factory=list,
        description="RAG 检索结果列表（按 COSINE similarity 降序，最相似在前；保持 Milvus 返回顺序；不含 embedding；含 Document 元数据）",
    )


# ---------------------------------------------------------------------------
# RAG 问答（/rag/ask）Schema —— Phase 3.3 Step 3 新增
# ---------------------------------------------------------------------------


class RagAskRequest(BaseModel):
    """
    POST /rag/ask 请求体。

    字段：
        query       : 用户问题（必须非空字符串）。
        document_id : 限定基于指定 Document 回答（当前网页模式）；None = 全部知识库模式。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        description="用户问题（必须非空）",
    )
    document_id: int | None = Field(
        default=None,
        gt=0,
        description="限定回答基于指定 Document（当前网页模式）；None=全部知识库模式",
    )


class RagAnswerSource(BaseModel):
    """
    AI 回答的引用来源 item。

    字段：
        document_id : 来源 Document ID（= page_id，1:1）。
        title       : 来源标题（网页剪藏；上传文档为 None）。
        url         : 来源 URL（网页剪藏；上传文档为 None）。
        chunk_id    : 来源 chunk ID（如 "53_0"；= RagSearchResult.id）。
        score       : 相似度分数（Milvus COSINE similarity，越大越相似；= RagSearchResult.distance）。
    """

    model_config = ConfigDict(extra="forbid")

    document_id: int = Field(
        ...,
        ge=1,
        description="来源 Document ID（= page_id，1:1）",
    )
    title: str | None = Field(
        default=None,
        max_length=512,
        description="来源标题（网页剪藏；上传文档为 None）",
    )
    url: str | None = Field(
        default=None,
        max_length=2048,
        description="来源 URL（网页剪藏；上传文档为 None）",
    )
    chunk_id: str = Field(
        ...,
        min_length=1,
        description="来源 chunk ID（如 \"53_0\"；= RagSearchResult.id）",
    )
    score: float = Field(
        ...,
        description="相似度分数（Milvus COSINE similarity，越大越相似；= RagSearchResult.distance）",
    )


class RagAskResponse(BaseModel):
    """
    POST /rag/ask 响应体。

    字段：
        answer  : AI 生成的回答文本；无检索结果时返回固定提示语（不调用 LLM）。
        sources : 回答引用的来源列表（来自真实 retrieval result；无结果时为空列表）。
    """

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(
        ...,
        min_length=1,
        description="AI 生成的回答文本（无检索结果时为固定提示语）",
    )
    sources: list[RagAnswerSource] = Field(
        default_factory=list,
        description="回答引用的来源列表（无检索结果时为空列表）",
    )


# ---------------------------------------------------------------------------
# Plugin Workspace Schema —— Phase 3.5 Step 2-D 新增
# ---------------------------------------------------------------------------


class PluginRegisterRequest(BaseModel):
    """
    POST /plugins/register 请求体（注册不需要任何 Plugin Header）。

    字段约束：
        - plugin_name：1-64 字符（Pydantic 层宽松上限）；
          具体规则（trim 后 2-32 字符 / 字符集 / 禁止控制字符）由
          PluginService.validate_plugin_name 统一校验（PluginNameValidationError → 422）。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Workspace 显示名（trim 后 2-32 字符；字符集由 PluginService 校验）",
    )


class PluginRegisterResponse(BaseModel):
    """
    POST /plugins/register 响应体。

    安全红线：plugin_secret 明文只在此响应中返回一次；不写 DB / 日志 /
    响应以外任何位置。客户端必须立即保存，后端不提供找回入口。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(..., min_length=1, description="Workspace 唯一标识")
    plugin_name: str = Field(
        ..., min_length=1, max_length=64, description="Workspace 显示名"
    )
    plugin_secret: str = Field(
        ..., min_length=1, description="Workspace 认证凭证（仅本次返回一次，请立即保存）"
    )


class PluginMeResponse(BaseModel):
    """
    GET /plugins/me 响应体（当前 Workspace 信息）。

    安全红线：绝不返回 plugin_secret / plugin_secret_hash /
    api_key_ciphertext / api_key_nonce / api_key 明文。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(..., min_length=1, description="Workspace 唯一标识")
    plugin_name: str = Field(
        ..., min_length=1, max_length=64, description="Workspace 显示名"
    )
    status: str = Field(..., description="Workspace 状态（ACTIVE / DISABLED）")
    api_key_configured: bool = Field(
        ...,
        description="是否已配置百炼 API Key（ciphertext 与 nonce 均非 NULL）",
    )
    created_at: datetime = Field(..., description="Workspace 创建时间（ISO 8601）")
    updated_at: datetime = Field(
        ..., description="Workspace 最近更新时间（ISO 8601）"
    )


class PluginUpdateNameRequest(BaseModel):
    """
    PUT /plugins/me 请求体：修改 Workspace 显示名（plugin_id 不变）。

    名称规则由 PluginService.validate_plugin_name 统一校验（与 register 一致）。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="新显示名（trim 后 2-32 字符；字符集由 PluginService 校验）",
    )


class PluginUpdateNameResponse(BaseModel):
    """
    PUT /plugins/me 响应体。

    必须保证：plugin_id 不变 / plugin_secret_hash 不变 / API Key 不变 /
    documents 不变。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(..., min_length=1, description="Workspace 唯一标识（改名不变）")
    plugin_name: str = Field(
        ..., min_length=1, max_length=64, description="新显示名"
    )


class PluginUpdateApiKeyRequest(BaseModel):
    """
    PUT /plugins/me/api-key 请求体：配置 / 更换 Workspace 的百炼 API Key。

    字段约束：
        - 必须以 "sk-" 开头（百炼 DashScope Key 格式；Pydantic 层 422）；
        - 真实有效性由 PluginService 调 EmbeddingClient 最小验证（失败 400）。
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(
        ...,
        min_length=1,
        max_length=512,
        pattern=r"^sk-",
        description="百炼 API Key（sk- 开头；配置后立即生效）",
    )


class PluginUpdateApiKeyResponse(BaseModel):
    """
    PUT /plugins/me/api-key 响应体。

    安全红线：不返回 api_key / api_key_ciphertext / api_key_nonce /
    plugin_secret 任何信息。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(..., min_length=1, description="Workspace 唯一标识")
    api_key_configured: bool = Field(
        ..., description="是否已配置百炼 API Key（ciphertext 与 nonce 均非 NULL）"
    )


class PluginDeleteRequest(BaseModel):
    """
    DELETE /plugins/me 请求体：危险操作双重确认。

    仅删除 plugin_workspaces 行；documents / Milvus / FileStorage 的
    级联删除属于后续 Step 2-E 的 Service 级联删除，本阶段不涉及。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(
        ..., description="必须为 true 才允许删除（防误触）"
    )
    plugin_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="当前 Workspace 显示名（必须与当前名称完全一致，防误删）",
    )
