"""create documents table

Revision ID: 0001
Revises:
Create Date: 2026-08-22

Phase 2.9 Step 1：创建 documents 表（Document 元数据 source of truth）。

字段定义严格对齐 backend.models.document.Document ORM：
- id          : INT PK AUTOINCREMENT
- user_id     : INT NULL（建索引，便于按 user 查询）
- filename    : VARCHAR(255) NOT NULL
- file_path   : VARCHAR(512) NOT NULL
- status      : VARCHAR(32) NOT NULL DEFAULT 'PENDING'（建索引）
- chunk_count : INT NOT NULL DEFAULT 0
- created_at  : DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
- updated_at  : DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP

设计要点：
- 手写 migration（非 autogenerate），保证 ORM 字段 ≡ DB 字段 ≡ migration 字段三对齐；
- status server_default 用 text("'PENDING'") 单引号包裹，避免 MySQL DDL 错误；
- 索引名与 ORM 自动命名对齐：ix_documents_user_id / ix_documents_status。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 documents 表 + 索引。"""
    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "filename",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "file_path",
            sa.String(length=512),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column(
            "chunk_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 索引名与 ORM 自动命名对齐（SQLAlchemy 默认 ix_<table>_<column>）
    op.create_index("ix_documents_user_id", "documents", ["user_id"])
    op.create_index("ix_documents_status", "documents", ["status"])


def downgrade() -> None:
    """删除 documents 表 + 索引。"""
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_table("documents")
