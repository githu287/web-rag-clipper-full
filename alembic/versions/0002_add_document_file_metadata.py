"""add document file metadata

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-23

Phase 2.10 Step 2：documents 表新增文件上传元数据字段。

字段定义严格对齐 backend.models.document.Document ORM 新增部分：
- file_size     : INT NOT NULL DEFAULT 0（文件大小字节）
- mime_type     : VARCHAR(128) NOT NULL DEFAULT ''（MIME 类型）
- error_message : TEXT NULL（FAILED 失败原因，仅失败路径写入）

设计要点：
- 手写 migration（非 autogenerate），保持 ORM 字段 ≡ DB 字段 ≡ migration 字段三对齐；
- file_size / mime_type 使用 server_default 兼容存量行（NOT NULL 且无显式值），
  server_default 与 ORM 层 default 一致（双层默认，与 status/chunk_count 同风格）；
- error_message 为 TEXT 可空，无需默认值；
- 不修改 0001；downgrade 反向删除三个字段。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 file_size / mime_type / error_message 三列。"""
    op.add_column(
        "documents",
        sa.Column(
            "file_size",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "mime_type",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "documents",
        sa.Column("error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """反向删除三个字段（先 error_message 后其余，顺序无关）。"""
    op.drop_column("documents", "error_message")
    op.drop_column("documents", "mime_type")
    op.drop_column("documents", "file_size")
