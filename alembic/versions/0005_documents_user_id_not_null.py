"""documents user_id not null

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-25

Phase 3.4 Step 3：documents.user_id 由可空升级为 NOT NULL，存量 NULL 数据归属
DISABLED migration user，历史数据不丢失。

步骤：
1) 创建/确认 migration user（status='DISABLED'，不可用于真实业务；占位
   api_key_hash / token_hash 使用固定非 SHA-256 字符串，绝不会与真实用户的
   64 位十六进制 hash 冲突）；
2) UPDATE documents SET user_id = <migration user id> WHERE user_id IS NULL；
3) ALTER user_id SET NOT NULL；
4) 新增复合索引 ix_documents_user_id_status(user_id, status)（全库 RAG 高频路径）。

设计要点：
- 先查后插保证重复执行安全（事务内，Alembic 默认事务）；
- migration user 的 id 动态读取，避免硬编码 id=1 假设；
- ALTER 用 batch_alter_table：SQLite（测试 / ALEMBIC_DATABASE_URL 覆盖）下自动
  走重建表路径，MySQL 下透明转发，两端兼容；
- 不修改 0001 / 0002 / 0003 / 0004；
- downgrade 仅还原 documents 结构，保留 migration user 行（0004 downgrade 删 users 表）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# migration user 占位标识：固定字符串，不是 SHA-256 十六进制，不可能与真实用户 hash 冲突
_MIGRATION_USER_API_KEY_HASH = "migration-disabled-user-api-key-hash"
_MIGRATION_USER_TOKEN_HASH = "migration-disabled-user-token-hash"


def upgrade() -> None:
    """存量 NULL 归属 migration user + user_id NOT NULL + 复合索引。"""
    bind = op.get_bind()

    # 1) 创建/确认 DISABLED migration user（幂等：先查后插）
    row = bind.execute(
        sa.text("SELECT id FROM users WHERE api_key_hash = :h"),
        {"h": _MIGRATION_USER_API_KEY_HASH},
    ).scalar_one_or_none()

    if row is None:
        bind.execute(
            sa.text(
                "INSERT INTO users"
                " (api_key_hash, api_key_ciphertext, api_key_nonce, token_hash, status)"
                " VALUES (:h, :c, :n, :t, 'DISABLED')"
            ),
            {
                "h": _MIGRATION_USER_API_KEY_HASH,
                # 占位内容：status=DISABLED，认证/解密链路永不触及
                "c": "migration-disabled-user",
                "n": "disabled",
                "t": _MIGRATION_USER_TOKEN_HASH,
            },
        )
        migration_user_id = bind.execute(
            sa.text("SELECT id FROM users WHERE api_key_hash = :h"),
            {"h": _MIGRATION_USER_API_KEY_HASH},
        ).scalar_one()
    else:
        migration_user_id = row

    # 2) 存量 NULL 数据归属 migration user（历史数据不丢失）
    bind.execute(
        sa.text("UPDATE documents SET user_id = :uid WHERE user_id IS NULL"),
        {"uid": migration_user_id},
    )

    # 3) user_id → NOT NULL（batch mode：SQLite 重建表 / MySQL 透明转发）
    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )

    # 4) 复合索引 (user_id, status)：全库 RAG 高频路径
    op.create_index(
        "ix_documents_user_id_status",
        "documents",
        ["user_id", "status"],
    )


def downgrade() -> None:
    """还原 documents：删除复合索引 + user_id 恢复可空。"""
    op.drop_index("ix_documents_user_id_status", table_name="documents")

    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
        )
    # migration user 行保留：0004 downgrade 会删除 users 表
