"""plugin workspace identity (Phase 3.5 Step 2-A)

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-26

Phase 3.5：Plugin Workspace 双凭证身份体系。

1) 创建 plugin_workspaces 表：
   - plugin_id          : VARCHAR(64) UNIQUE NOT NULL，后端生成的 Workspace 标识（非秘密）
   - plugin_name        : VARCHAR(64) NOT NULL，用户可理解的展示名称
   - plugin_name_norm   : VARCHAR(64) UNIQUE NOT NULL，归一化后名称（strip → 连续空白压缩 → lower）
   - plugin_secret_hash : CHAR(64) UNIQUE NOT NULL，SHA-256(plugin_secret)，数据库绝不存明文 secret
   - api_key_ciphertext : VARCHAR(1024) NULL，AES-256-GCM 密文（仅模型服务凭证，不参与身份）
   - api_key_nonce      : VARCHAR(32) NULL
   - status             : VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'（ACTIVE / DISABLED / DELETING）
2) documents 新增 plugin_id VARCHAR(64) NULL；
3) 历史数据迁移：一个旧 user → 一个新 Plugin Workspace，
   documents.user_id → documents.plugin_id 回填（document.id / page_id / Milvus 不变）；
4) plugin_id → NOT NULL + FK（→ plugin_workspaces.plugin_id）+ 复合索引
   ix_documents_plugin_id_status(plugin_id, status)。

安全约束（严格执行）：
- plugin_secret 仅内存生成，立即计算 SHA-256 后只存哈希；
  明文 secret 不写日志 / stdout / stderr / 普通文件 / 明文入库；
- 迁移产生的 legacy workspace 不输出明文 secret（用户无法自动持有；
  不为了"迁移后可登录"把 secret 输出到日志）；
- 不删除任何 users / documents 行，不动 Milvus 数据；
- users 表保留（观察期），旧 migration 文件保留（可回滚）；
- documents.user_id 列保留（回滚安全），功能层在后续阶段废弃。

跨库策略（对齐 0005 / 0006）：
- 全部 ALTER 使用 op.batch_alter_table：SQLite（ALEMBIC_DATABASE_URL 验证）自动重建表，
  MySQL 透明转发 ALTER；数据填充在 batch 之外用参数化 SQL 完成。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import hashlib
import secrets


# revision identifiers
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _generate_plugin_id() -> str:
    """Workspace 标识：secrets.token_urlsafe(32) = 43 字符，不可枚举。"""
    return secrets.token_urlsafe(32)


def _hash_secret(secret: str) -> str:
    """plugin_secret → SHA-256 hex（64 字符）。仅哈希入库，绝不存明文。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _normalize_name(name: str) -> str:
    """plugin_name 归一化：strip → 连续空白压缩为一个空格 → lower。"""
    return " ".join(name.strip().split()).lower()


def _next_available_name(base_name: str, used_norms: set) -> tuple[str, str]:
    """返回 (plugin_name, plugin_name_norm)；归一化冲突时追加 -2 / -3 后缀。"""
    norm = _normalize_name(base_name)
    if norm not in used_norms:
        return base_name, norm
    suffix = 2
    while True:
        candidate = f"{base_name}-{suffix}"
        cnorm = _normalize_name(candidate)
        if cnorm not in used_norms:
            return candidate, cnorm
        suffix += 1


def upgrade() -> None:
    """plugin_workspaces 表 + documents.plugin_id + 历史数据回填 + 索引/FK。"""
    bind = op.get_bind()

    # 1) plugin_workspaces 表（字段严格对齐 backend.models.plugin.PluginWorkspace ORM）
    op.create_table(
        "plugin_workspaces",
        # id：需求 BIGINT PK；SQLite 仅 INTEGER PRIMARY KEY 自动 rowid，
        # 用 with_variant 保证 SQLite 验证环境自增可用，MySQL 保持 BIGINT。
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("plugin_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_name", sa.String(length=64), nullable=False),
        sa.Column("plugin_name_norm", sa.String(length=64), nullable=False),
        sa.Column("plugin_secret_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("api_key_ciphertext", sa.String(length=1024), nullable=True),
        sa.Column("api_key_nonce", sa.String(length=32), nullable=True),
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
        sa.UniqueConstraint("plugin_id", name="uq_plugin_workspaces_plugin_id"),
        sa.UniqueConstraint("plugin_name_norm", name="uq_plugin_workspaces_plugin_name_norm"),
        sa.UniqueConstraint("plugin_secret_hash", name="uq_plugin_workspaces_plugin_secret_hash"),
    )

    # 2) documents.plugin_id：先可空（历史回填后再收紧）
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("plugin_id", sa.String(length=64), nullable=True))

    # 3) 历史数据迁移：一个旧 user → 一个新 Plugin Workspace。
    #    覆盖 users 表全部用户 + documents 中孤儿 user_id 兜底（documents.user_id 无 FK，
    #    理论上可能存在不属于任何 user 的行，迁移时兜底归集，保证 documents 不丢失）。
    user_rows = bind.execute(
        sa.text("SELECT id, username FROM users ORDER BY id")
    ).fetchall()
    users: dict = {row[0]: row[1] for row in user_rows}
    doc_user_ids = set(
        bind.execute(sa.text("SELECT DISTINCT user_id FROM documents")).scalars()
    )
    all_user_ids = sorted(set(users) | doc_user_ids)

    used_norms: set = set()
    user_id_to_plugin_id: dict = {}

    for uid in all_user_ids:
        base_name = users.get(uid) or f"legacy_{uid}"
        plugin_name, norm = _next_available_name(base_name, used_norms)
        plugin_id = _generate_plugin_id()
        # plugin_secret：仅内存生成 → 立即 SHA-256 → 只存哈希。
        # 明文 secret 不出本函数、不落库、不打日志、不写 stdout（安全红线）。
        secret = _generate_plugin_id()
        secret_hash = _hash_secret(secret)
        bind.execute(
            sa.text(
                "INSERT INTO plugin_workspaces"
                " (plugin_id, plugin_name, plugin_name_norm, plugin_secret_hash, status)"
                " VALUES (:pid, :pname, :pnorm, :shash, 'ACTIVE')"
            ),
            {"pid": plugin_id, "pname": plugin_name, "pnorm": norm, "shash": secret_hash},
        )
        used_norms.add(norm)
        user_id_to_plugin_id[uid] = plugin_id

    # 4) documents.plugin_id 回填（document.id / page_id / Milvus 不变）
    for uid, plugin_id in user_id_to_plugin_id.items():
        bind.execute(
            sa.text("UPDATE documents SET plugin_id = :pid WHERE user_id = :uid"),
            {"pid": plugin_id, "uid": uid},
        )

    # 5) 收紧：plugin_id NOT NULL + 复合索引 + FK（SQLite batch 重建表，MySQL 透明转发）
    with op.batch_alter_table("documents") as batch_op:
        batch_op.alter_column(
            "plugin_id",
            existing_type=sa.String(length=64),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.create_index("ix_documents_plugin_id_status", ["plugin_id", "status"])
        batch_op.create_foreign_key(
            "fk_documents_plugin_id_plugin_workspaces",
            "plugin_workspaces",
            ["plugin_id"],
            ["plugin_id"],
        )


def downgrade() -> None:
    """还原到 0006 结构（结构级；回填的 plugin_id 值随列删除，不影响 user_id 数据）。"""
    # 先删 FK / 索引，再删列（MySQL 中带 FK 的列不能直接 drop）
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_constraint(
            "fk_documents_plugin_id_plugin_workspaces", type_="foreignkey"
        )
        batch_op.drop_index("ix_documents_plugin_id_status")
        batch_op.drop_column("plugin_id")
    op.drop_table("plugin_workspaces")
