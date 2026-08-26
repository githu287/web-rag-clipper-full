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
                      None = 全部知识库模式。ownership 由后端 user_id 校验，
                      不允许客户端通过 document_id 访问他人文档。
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
# Auth / 用户身份 Schema —— Phase 3.4 Step 4 新增
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """
    POST /auth/register 请求体（F-REV3：注册仅需 username + password）。

    注册完全不依赖百炼 API Key：账号身份与模型配置解耦，用户注册成功后
    可随时在 PUT /users/me/api-key 配置模型 Key。

    字段约束：
        - username：1-64 字符，仅允许字母数字与 _ . -（Pydantic 层 422）；
        - password：1-128 字符；长度强度（8-128）由 Service 层
          validate_password_strength 校验（PasswordPolicyError → 422）。
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_.\-]+$",
        description="用户名（1-64 字符；字母数字与 _ . -）",
    )
    password: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="登录密码（8-128 字符；强度由后端校验）",
    )


class LoginRequest(BaseModel):
    """
    POST /auth/login 请求体（F-REV3：登录仅需 username + password）。

    username 不存在与密码错误统一 401（防用户枚举），由 Service 层保证。
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_.\-]+$",
        description="用户名",
    )
    password: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="登录密码",
    )


class AuthResponse(BaseModel):
    """
    注册 / 登录成功响应体。

    字段：
        user_id   : users.id 主键（客户端后续无需再传，服务端以 Bearer token 识别）。
        token     : opaque Bearer token（明文仅本次返回一次；DB 只存 SHA-256 hash）。
        token_type: 固定 "Bearer"（客户端拼 Authorization: Bearer <token>）。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(..., ge=1, description="用户主键 ID")
    token: str = Field(..., min_length=1, description="opaque Bearer token（仅返回一次）")
    token_type: str = Field(default="Bearer", description="Token 类型（固定 Bearer）")


class ApiKeyUpdateRequest(BaseModel):
    """
    PUT /users/me/api-key 请求体：已登录用户配置 / 更换自己的百炼 API Key。

    更换后：
        - api_key_ciphertext / nonce 更新为新 Key 的加密副本（AES-256-GCM）；
        - token 不变（用户身份不变）、user_id 不变、username 不变、
          password_hash 不变、documents 归属不变；
        - 后续所有 embedding / LLM 调用自动使用新 Key。

    字段约束：
        - 必须以 "sk-" 开头（百炼 DashScope Key 格式；Pydantic 层 422）；
        - 真实有效性由 UserService 调 EmbeddingClient 最小验证（失败 400）。
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(
        ...,
        min_length=1,
        max_length=512,
        pattern=r"^sk-",
        description="百炼 API Key（sk- 开头；配置后立即生效）",
    )


class ApiKeyUpdateResponse(BaseModel):
    """
    PUT /users/me/api-key 响应体。

    字段：
        user_id: 被配置 Key 的用户主键（token 不变，客户端继续用原 token）。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(..., ge=1, description="用户主键 ID")


class UserMeResponse(BaseModel):
    """
    GET /users/me 响应体（当前用户信息）。

    安全红线：绝不返回 api_key / api_key_ciphertext / api_key_nonce /
    password_hash / token_hash / APP_MASTER_KEY。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(..., ge=1, description="用户主键 ID")
    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    api_key_configured: bool = Field(
        ...,
        description="是否已配置百炼 API Key（ciphertext 与 nonce 均非 NULL）",
    )
    created_at: datetime = Field(..., description="用户创建时间（ISO 8601）")
