"""
Document API Schema（Phase 2.9 Step 3）。

定义 Document 生命周期 HTTP 接口的请求 / 响应 Pydantic Schema：

    POST /documents                 → DocumentCreateRequest / DocumentResponse
    POST /documents/{id}/ingest     → DocumentIngestRequest / DocumentIngestResponse

设计要点：
1) 与内部 ORM（models/document.py）解耦：DocumentResponse 只暴露 HTTP 边界所需
   字段（id / filename / file_path / status / chunk_count），不暴露时间戳与内部细节；
2) 请求不得直接传 embedding（向量由 IngestService 内部经 EmbeddingClient 计算），
   因此本文件不定义任何 embedding 字段，也不复用 models/milvus_dto.ChunkVector；
3) 全部 Schema 使用 ConfigDict(extra="forbid")，与 api_schema.py 既有风格一致，
   拒绝未声明字段，防止契约漂移；
4) plugin_id 不由客户端传入（Phase 3.5 Step 2-B 起由 X-Plugin-ID 头决定归属；
   POST /documents 的归属 = current_plugin.plugin_id）。

异常契约（由 main.py 全局 handler 转换）：
    - DocumentNotFoundError → 404
    - DocumentOperationError → 503
    - EmbeddingClientError → 502（复用既有 handler）
    - MilvusRepositoryError → 503（复用既有 handler）
    - 422 由 Pydantic / FastAPI 校验自动产生（chunks 为空、字段缺失等）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 创建 Document 接口 Schema
# ---------------------------------------------------------------------------


class DocumentCreateRequest(BaseModel):
    """
    POST /documents 请求体。

    字段：
        filename : 文件名（非空，<=255 字符）。
        file_path: 文件存储路径（非空，<=512 字符）。

    归属（Phase 3.5 Step 2-B）：plugin_id 不由客户端传入，Document 归属 =
    当前 Plugin（X-Plugin-ID 解析的 current_plugin.plugin_id）。
    """

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="文件名（非空，<=255 字符）",
    )
    file_path: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="文件存储路径（非空，<=512 字符）",
    )


class DocumentResponse(BaseModel):
    """
    POST /documents 响应体。

    字段与数据库 documents 表核心字段对齐；不含 created_at / updated_at /
    plugin_id 等内部细节（HTTP 边界最小契约）。
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="Document 主键 ID（1:1 对应 Milvus page_id）")
    filename: str = Field(..., description="文件名")
    file_path: str = Field(..., description="文件存储路径")
    status: str = Field(..., description="当前状态（PENDING/PROCESSING/SUCCESS/FAILED/DELETING）")
    chunk_count: int = Field(..., description="chunk 数量（创建时为 0）")


# ---------------------------------------------------------------------------
# Document Ingest 接口 Schema
# ---------------------------------------------------------------------------


class DocumentIngestRequest(BaseModel):
    """
    POST /documents/{document_id}/ingest 请求体。

    字段：
        chunks : 已切分的 chunk 文本列表（必须非空；向量由服务内部计算，不在此传入）。
    """

    model_config = ConfigDict(extra="forbid")

    chunks: list[str] = Field(
        ...,
        min_length=1,
        description="已切分的 chunk 文本列表（必须非空）",
    )


class DocumentIngestResponse(BaseModel):
    """
    POST /documents/{document_id}/ingest 响应体。

    字段：
        document_id: Document 主键 ID（= Milvus page_id，方案 A）。
        status     : ingest 完成后的 Document 终态（成功为 SUCCESS）。
        chunk_count: ingest 完成后的 chunk 总数。
    """

    model_config = ConfigDict(extra="forbid")

    document_id: int = Field(..., description="Document 主键 ID（= Milvus page_id）")
    status: str = Field(..., description="ingest 完成后的 Document 状态（SUCCESS）")
    chunk_count: int = Field(..., description="ingest 完成后的 chunk 总数")


# ---------------------------------------------------------------------------
# Document Upload 接口 Schema
# ---------------------------------------------------------------------------


class DocumentUploadResponse(BaseModel):
    """
    POST /documents/upload 响应体（Phase 2.10 Step 3）。

    字段与 documents 表核心字段对齐，供客户端确认上传结果：
        id           : Document 主键 ID（1:1 对应 Milvus page_id）。
        filename     : 原始文件名。
        file_size    : 文件字节数。
        mime_type    : MIME 类型（上传未识别时为空串）。
        status       : 终态（成功为 SUCCESS；失败路径由异常返回，不走本响应）。
        chunk_count  : 入库 chunk 总数（成功时为实际数量）。
        error_message: 失败摘要（成功时为 None；本响应正常路径恒为 None）。

    不返回：embedding / chunk 全量内容 / Milvus 内部字段（HTTP 边界最小契约）。
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="Document 主键 ID（1:1 对应 Milvus page_id）")
    filename: str = Field(..., description="文件名")
    file_size: int = Field(..., description="文件字节数")
    mime_type: str = Field(..., description="MIME 类型（未识别时为空串）")
    status: str = Field(..., description="终态（成功为 SUCCESS）")
    chunk_count: int = Field(..., description="入库 chunk 总数")
    error_message: str | None = Field(
        default=None,
        description="失败摘要（成功时为 None）",
    )


# ---------------------------------------------------------------------------
# Web Clip 接口 Schema（Phase 3.1 Step 3）
# ---------------------------------------------------------------------------


class WebClipRequest(BaseModel):
    """
    POST /clips 请求体（Phase 3.1 Step 3）。

    由未来 Browser Extension 发送网页 URL / 标题 / 正文纯文本；后端完成
    Document 创建 → Chunker → Embedding → Milvus → SUCCESS 全链路。

    字段：
        url     : 网页来源 URL（非空，<=2048 字符）。
        title   : 网页标题（可选，<=512 字符；None 时写入 NULL）。
        raw_text: 网页正文纯文本（非空）。

    不允许客户端传入 source_type —— 后端固定为 "webpage"（extra="forbid" 拒绝
    未声明字段，防止契约漂移）。
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        description="网页来源 URL（非空，<=2048 字符）",
    )
    title: str | None = Field(
        default=None,
        max_length=512,
        description="网页标题（可选，<=512 字符）",
    )
    raw_text: str = Field(
        ...,
        min_length=1,
        description="网页正文纯文本（非空）",
    )


class WebClipResponse(BaseModel):
    """
    POST /clips 响应体（HTTP 201）。

    字段与 documents 表核心字段对齐，并新增网页来源元数据：
        id          : Document 主键 ID（1:1 对应 Milvus page_id）。
        filename    : 固定为 "webclip.txt"（WebClip 不创建物理文件）。
        status      : 终态（成功为 SUCCESS）。
        chunk_count : 入库 chunk 总数。
        error_message: 失败摘要（成功时为 None）。
        title       : 网页标题（可空）。
        url         : 网页来源 URL。
        source_type : 固定为 "webpage"。
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="Document 主键 ID（1:1 对应 Milvus page_id）")
    filename: str = Field(..., description="文件名（固定 webclip.txt）")
    status: str = Field(..., description="终态（成功为 SUCCESS）")
    chunk_count: int = Field(..., description="入库 chunk 总数")
    error_message: str | None = Field(
        default=None,
        description="失败摘要（成功时为 None）",
    )
    title: str | None = Field(
        default=None,
        description="网页标题（可空）",
    )
    url: str | None = Field(
        default=None,
        description="网页来源 URL",
    )
    source_type: str = Field(
        ...,
        description="来源类型（固定 webpage）",
    )


# ---------------------------------------------------------------------------
# 「我的知识库」列表 / 详情接口 Schema（Phase 3.6 Step 2-A）
# ---------------------------------------------------------------------------


# 查询筛选枚举（白名单 Literal，非法值由 FastAPI 直接 422）：
# - status 刻意排除 DELETING（删除中的瞬态，不作为独立筛选项，UI 归入「处理中」）；
# - source_type 仅 upload / webpage 两种真实来源。
DocumentStatusFilter = Literal["PENDING", "PROCESSING", "SUCCESS", "FAILED"]
DocumentSourceTypeFilter = Literal["upload", "webpage"]


class DocumentSummaryResponse(BaseModel):
    """
    GET /documents 列表项（安全白名单，Phase 3.6 Step 2-A）。

    只暴露列表业务字段；刻意不包含：plugin_id、file_path、created_at 以外的
    内部细节，以及任何认证/加密内部字段（plugin_secret / plugin_secret_hash /
    api_key_ciphertext / api_key_nonce / APP_MASTER_KEY）—— 白名单模型天然
    拒绝未声明字段外泄。
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="Document 主键 ID（1:1 对应 Milvus page_id）")
    title: str | None = Field(default=None, description="网页标题（上传文档为 None）")
    filename: str = Field(..., description="文件名")
    url: str | None = Field(default=None, description="来源 URL（上传文档为 None）")
    source_type: str = Field(..., description="来源类型（upload / webpage）")
    status: str = Field(..., description="当前状态（PENDING/PROCESSING/SUCCESS/FAILED/DELETING）")
    chunk_count: int = Field(..., description="chunk 数量（创建时为 0）")
    file_size: int = Field(..., description="文件字节数（网页剪藏为 0）")
    error_message: str | None = Field(default=None, description="失败摘要（成功时为 None）")
    created_at: datetime = Field(..., description="创建时间")


class DocumentDetailResponse(DocumentSummaryResponse):
    """
    GET /documents/{document_id} 详情（Phase 3.6 Step 2-A）。

    在列表白名单基础上追加详情字段：mime_type / updated_at。
    同样刻意不包含 plugin_id / file_path 及任何认证/加密内部字段。
    """

    mime_type: str = Field(..., description="MIME 类型（未识别时为空串）")
    updated_at: datetime = Field(..., description="更新时间")


class DocumentListResponse(BaseModel):
    """
    GET /documents 分页响应（Phase 3.6 Step 2-A）。

    固定结构：{items, total, page, page_size, pages}。
    pages 由 Router 计算：ceil(total / page_size)；total=0 时 pages=0。
    """

    model_config = ConfigDict(extra="forbid")

    items: list[DocumentSummaryResponse] = Field(..., description="当前页文档列表")
    total: int = Field(..., description="匹配条件的文档总数")
    page: int = Field(..., description="当前页码（>=1）")
    page_size: int = Field(..., description="每页条数")
    pages: int = Field(..., description="总页数（total=0 时为 0）")
