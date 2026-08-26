"""user identity rework: username + password, drop api_key_hash

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-26

Phase 3.4 F-REV3：用户身份模型最终重构（API Key 完全退出身份体系）。

users 表迁移（对齐 backend.models.user.User ORM）：
1.  ADD username VARCHAR(64) NULL；
2.  ADD password_hash VARCHAR(255) NULL；
3.  存量 users 填充：username = 'legacy_<id>'，password_hash = 随机 Argon2id 哨兵；
4.  username → NOT NULL；
5.  username → UNIQUE（uq_users_username）；
6.  password_hash → NOT NULL；
7.  token_hash → NULLABLE（支持 logout 置 NULL）；
8.  api_key_ciphertext → NULLABLE（支持未配置 Key 用户）；
9.  api_key_nonce → NULLABLE；
10. DROP api_key_hash（MySQL 列级 UNIQUE 独立索引名 = 列名，DROP COLUMN 自动携带删除；
    SQLite 重建表自动排除该列及其约束）。

安全约束：
- 不删除任何 users / documents 行，不动 Milvus 数据；
- 不输出明文 API Key / token / password 到日志；
- 存量用户 username 用 legacy_<id>，与任何未来真实 username 不冲突；
- 存量 password_hash 为随机 Argon2id 哨兵（不可登录，符合安全迁移约定）。

跨库策略（对齐 0005）：
- 全部 ALTER 使用 op.batch_alter_table：SQLite（ALEMBIC_DATABASE_URL 验证）自动重建表，
  MySQL 透明转发 ALTER；数据填充在 batch 之外用参数化 SQL 完成。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from argon2 import PasswordHasher
import secrets

# revision identifiers
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _sentinel_password_hash() -> str:
    """生成随机 Argon2id 哨兵哈希（不可登录，仅满足 password_hash NOT NULL）。"""
    return PasswordHasher().hash(secrets.token_urlsafe(32))


def upgrade() -> None:
    """迁移 users 到 username + password 身份模型。"""
    bind = op.get_bind()

    # 1) 新增可空列（先允许 NULL，填充数据后再收紧约束）
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("username", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("password_hash", sa.String(length=255), nullable=True)
        )

    # 2) 存量 users 填充（不删除任何数据；参数化 SQL，不拼接用户输入）
    legacy_ids = list(
        bind.execute(sa.text("SELECT id FROM users WHERE username IS NULL")).scalars()
    )
    for uid in legacy_ids:
        bind.execute(
            sa.text(
                "UPDATE users SET username = :u, password_hash = :p WHERE id = :id"
            ),
            {"u": f"legacy_{uid}", "p": _sentinel_password_hash(), "id": uid},
        )

    # 3) 收紧约束 + 解除 API Key 列强约束 + 删除 api_key_hash
    with op.batch_alter_table("users") as batch_op:
        # username：NOT NULL + UNIQUE（身份入口）
        batch_op.alter_column(
            "username",
            existing_type=sa.String(length=64),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.create_unique_constraint("uq_users_username", ["username"])
        # password_hash：NOT NULL（Argon2id 哈希）
        batch_op.alter_column(
            "password_hash",
            existing_type=sa.String(length=255),
            existing_nullable=True,
            nullable=False,
        )
        # token_hash：UNIQUE 保留，解除 NOT NULL（logout 置 NULL）
        batch_op.alter_column(
            "token_hash",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            nullable=True,
        )
        # API Key 列：解除 NOT NULL（未配置 Key 用户 = NULL）
        batch_op.alter_column(
            "api_key_ciphertext",
            existing_type=sa.String(length=1024),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.alter_column(
            "api_key_nonce",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            nullable=True,
        )
        # 删除 api_key_hash（MySQL：DROP COLUMN 自动携带 UNIQUE 索引；
        # SQLite：重建表自动排除该列及其约束）
        batch_op.drop_column("api_key_hash")


def downgrade() -> None:
    """还原 users 到 0005 结构（结构级；真实 api_key_hash 值无法恢复，用占位填充）。

    限制（文档化）：
    - api_key_hash 用固定占位字符串填充（满足 NOT NULL），不包含任何真实 Key 信息；
    - 若存在 token_hash / api_key_ciphertext / api_key_nonce 为 NULL 的行
      （logout 后 / 未配置 Key），恢复 NOT NULL 会失败——降级仅建议在开发环境使用，
      生产降级前需人工评估数据状态。
    """
    bind = op.get_bind()

    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("api_key_hash", sa.String(length=64), nullable=True)
        )

    bind.execute(
        sa.text("UPDATE users SET api_key_hash = :h WHERE api_key_hash IS NULL"),
        {"h": "migration-disabled-user-api-key-hash"},
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "api_key_hash",
            existing_type=sa.String(length=64),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.create_unique_constraint("api_key_hash", ["api_key_hash"])
        batch_op.alter_column(
            "token_hash",
            existing_type=sa.String(length=64),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "api_key_ciphertext",
            existing_type=sa.String(length=1024),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "api_key_nonce",
            existing_type=sa.String(length=32),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.drop_constraint("uq_users_username", type_="unique")
        batch_op.drop_column("username")
        batch_op.drop_column("password_hash")
