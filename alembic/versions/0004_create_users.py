"""create users table

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-25

Phase 3.4 Step 3：创建 users 表（用户身份 + API Key 加密存储 + token 哈希）。

字段定义严格对齐 backend.models.user.User ORM：
- id                 : INT PK AUTOINCREMENT
- api_key_hash       : VARCHAR(64) NOT NULL UNIQUE（SHA-256(api_key) 十六进制）
- api_key_ciphertext : VARCHAR(1024) NOT NULL（AES-256-GCM 密文 + tag，base64）
- api_key_nonce      : VARCHAR(32) NOT NULL（每条记录独立 12B 随机 nonce，base64）
- token_hash         : VARCHAR(64) NOT NULL UNIQUE（SHA-256(opaque random token) 十六进制）
- status             : VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'（ACTIVE / DISABLED）
- created_at         : DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
- updated_at         : DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP

设计要点：
- 手写 migration（非 autogenerate），保持 ORM 字段 ≡ DB 字段 ≡ migration 字段三对齐；
- api_key_hash / token_hash 列级 unique=True，与 ORM mapped_column(unique=True) 对齐
  （MySQL 默认唯一索引名 = 列名）；
- 不存储明文 API Key / token；密文 + 独立 nonce + hash 均非明文；
- 不加入 username / password / email / JWT / refresh_token（认证模型明确禁止）；
- status server_default 用 text("'ACTIVE'") 单引号包裹，避免 MySQL DDL 错误；
- 不修改 0001 / 0002 / 0003。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建 users 表。"""
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "api_key_hash",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "api_key_ciphertext",
            sa.String(length=1024),
            nullable=False,
        ),
        sa.Column(
            "api_key_nonce",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'ACTIVE'"),
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


def downgrade() -> None:
    """删除 users 表。"""
    op.drop_table("users")
