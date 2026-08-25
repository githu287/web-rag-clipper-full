"""
Document ORM 模型与状态常量（Phase 2.9 Step 1；Phase 2.11 Step 2 增 DELETING 状态；
Phase 3.4 Step 3 user_id 升级 NOT NULL）。

职责：
    定义 documents 表的 SQLAlchemy 2.0 ORM 映射 + DocumentStatus 状态常量集。

范围边界：
    - 仅数据层 ORM 映射；不接 ingest pipeline / API。
    - Document.id ↔ Milvus page_id 采用已确认方案 A（1:1）：document 级删除由
      DocumentDeleteService（Phase 2.11 Step 2）编排 query_page_chunks(page_id) +
      delete_chunks(ids) 完成 Milvus 侧联动，不依赖新增 document_id 标量字段、
      不修改 Milvus Schema。
    - 不定义 relationship（当前无外键关联表）。

设计要点：
1) status 字段同时给 default（Python 层）与 server_default（DB 层），保证 ORM 创建
   与裸 SQL INSERT 两种方式均有默认值；server_default 用 text("'PENDING'") 避免
   MySQL 默认值生成错误。
2) chunk_count 同样双层默认（default=0 + server_default="0"）。
3) 不使用 SQLAlchemy Enum 类型（避免 migration 痛点），用 String(32) + 应用层常量。
4) DELETING（Phase 2.11 Step 2）：删除已启动但未提交的中间状态，用于
   Delete ↔ Ingest/Retry 并发互斥；MySQL document 行删除成功才进入「不存在」终态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DocumentStatus:
    """Document 状态常量集（供 Repository / Service / API 共享校验）。"""

    PENDING: Final[str] = "PENDING"
    PROCESSING: Final[str] = "PROCESSING"
    SUCCESS: Final[str] = "SUCCESS"
    FAILED: Final[str] = "FAILED"
    # Phase 2.11 Step 2：删除进行中（删除已启动但未提交；禁止 ingest/retry 进入）
    DELETING: Final[str] = "DELETING"

    # 全部合法状态集合，供后续状态机校验（如 update_status 前置校验）
    ALL: Final[frozenset[str]] = frozenset(
        {PENDING, PROCESSING, SUCCESS, FAILED, DELETING}
    )


class DocumentSourceType:
    """Document 来源类型常量集（Phase 3.1 Step 3，供 Repository / Service 共享）。"""

    # 上传文件（默认值；既有上传链路兼容，不做任何迁移改动）
    UPLOAD: Final[str] = "upload"
    # 网页剪藏（WebClipService 固定使用，不允许客户端传入）
    WEBPAGE: Final[str] = "webpage"

    # 全部合法来源类型集合，供后续校验使用
    ALL: Final[frozenset[str]] = frozenset({UPLOAD, WEBPAGE})


class Document(Base):
    """documents 表 ORM：文档级元数据 source of truth。"""

    __tablename__ = "documents"
    # Phase 3.4 Step 3：全库 RAG 高频路径 (user_id, status) 复合索引；与 migration 0005 对齐
    __table_args__ = (
        Index("ix_documents_user_id_status", "user_id", "status"),
    )

    # 主键
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 所属用户（Phase 3.4 Step 3：NOT NULL；存量 NULL 历史数据在 migration 0005 归并到
    # DISABLED migration user（id=1），真实业务 user_id 一律来自 current_user.id）
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # 文件名与存储路径
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)

    # 状态：Python 层 default + DB 层 server_default；text("'PENDING'") 保证 MySQL DDL 正确
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DocumentStatus.PENDING,
        server_default=text("'PENDING'"),
        index=True,
    )

    # chunk 数量：双层默认（ORM 创建与裸 SQL INSERT 均默认 0）
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    # ---- Phase 2.10 Step 2 新增：文件上传元数据 ----
    # 文件大小（字节）：双层默认 0，兼容存量行（migration 0002 server_default="0"）
    file_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # MIME 类型（如 text/plain / text/markdown）：nullable=False 需默认值兼容存量行
    mime_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
        server_default="",
    )
    # FAILED 失败原因（Phase 0 架构 L149 设计：记录 error_message 便于重试补偿）；
    # 仅失败路径写入，成功/处理中为 None
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Phase 3.1 Step 3 新增：网页来源元数据 ----
    # 剪藏标题（如网页 <title>）；上传文档为 None
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 剪藏来源 URL；上传文档为 None
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # 来源类型：upload（上传文件，默认）/ webpage（网页剪藏）；
    # nullable=False + 双层默认 'upload' 兼容既有上传链路与存量行（migration 0003 server_default="upload"）
    source_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DocumentSourceType.UPLOAD,
        server_default=text("'upload'"),
        index=True,
    )

    # 时间戳：DB 层 server_default=func.now()；updated_at 每次 UPDATE 刷新
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
