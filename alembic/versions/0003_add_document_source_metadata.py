"""add document source metadata

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-25

Phase 3.1 Step 3：documents 表新增网页剪藏来源元数据字段。

字段定义严格对齐 backend.models.document.Document ORM 新增部分：
- title       : VARCHAR(512) NULL（剪藏标题，如网页 <title>）
- url         : VARCHAR(2048) NULL（剪藏来源 URL）
- source_type : VARCHAR(32) NOT NULL DEFAULT 'upload'（来源类型：upload / webpage）

设计要点：
- 手写 migration（非 autogenerate），保持 ORM 字段 ≡ DB 字段 ≡ migration 字段三对齐；
- source_type 使用 server_default="upload" 兼容存量行（NOT NULL 且无显式值，
  既有上传链路无需任何改动即默认 upload）；server_default 与 ORM 层 default 一致
  （双层默认，与 status/chunk_count/file_size 同风格）；
- title / url 为可空列，无需默认值；
- 不修改 0001 / 0002；downgrade 反向删除三个字段。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 title / url / source_type 三列。"""
    op.add_column(
        "documents",
        sa.Column("title", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "source_type",
            sa.String(length=32),
            nullable=False,
            server_default="upload",
        ),
    )


def downgrade() -> None:
    """反向删除三个字段。"""
    op.drop_column("documents", "source_type")
    op.drop_column("documents", "url")
    op.drop_column("documents", "title")
