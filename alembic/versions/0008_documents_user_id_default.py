"""documents user_id server default (Phase 3.5 遗留修复)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-27

Phase 3.5 Step 2-B 遗留缺陷修复（真实 E2E 发现，MySQL error 1364）：

背景链：
- migration 0005 将 documents.user_id 升级为 NOT NULL，且未设默认值；
- migration 0007 新增 plugin_id 作为功能层归属字段，但按"回滚安全"设计
  保留 user_id 列，同时 ORM 不再映射该列；
- 因此 WebClip / Upload 链路 create_document 的 ORM INSERT 不提供 user_id，
  MySQL 因 NOT NULL 且无默认值拒绝插入 → error 1364
  "Field 'user_id' doesn't have a default value"。

本迁移：ALTER documents.user_id 增加 server_default '0'（保持 NOT NULL），
使列继续保留（回滚安全）且 INSERT 不再失败；功能层归属仍完全以
plugin_id 为准（ORM 不映射 user_id，业务代码不读写该列）。

跨库策略（对齐 0005 / 0006 / 0007）：
- 使用 op.batch_alter_table：SQLite（ALEMBIC_DATABASE_URL 验证）自动重建表，
  MySQL 透明转发 ALTER。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """documents.user_id 增加 server_default '0'（保持 NOT NULL）。"""
    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default="0",
        )


def downgrade() -> None:
    """还原：移除 documents.user_id 的 server_default（回到 0007 状态）。"""
    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=None,
        )
