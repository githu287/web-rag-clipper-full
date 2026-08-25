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
4) user_id 不由客户端传入（Phase 3.4 Step 4 起由 Bearer token 决定归属；
   POST /documents 的归属 = current_user.id）。

异常契约（由 main.py 全局 handler 转换）：
    - DocumentNotFoundError → 404
    - DocumentOperationError → 503
    - EmbeddingClientError → 502（复用既有 handler）
    - MilvusRepositoryError → 503（复用既有 handler）
    - 422 由 Pydantic / FastAPI 校验自动产生（chunks 为空、字段缺失等）。
"""

from __future__ import annotations

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

    归属（Phase 3.4 Step 4）：user_id 不由客户端传入，Document 归属 =
    当前登录用户（Bearer token 解析的 current_user.id）。
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
    user_id 等内部细节（HTTP 边界最小契约）。
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
