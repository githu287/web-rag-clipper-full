"""expand encrypted model configuration storage

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "plugin_workspaces",
        "api_key_ciphertext",
        existing_type=sa.String(length=1024),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.add_column(
        "plugin_workspaces",
        sa.Column("embedding_config_fingerprint", sa.String(length=64), nullable=True),
    )
    legacy_value = "\0".join(
        (
            "dashscope",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "text-embedding-v3",
            "1",
            "1024",
        )
    )
    legacy_fingerprint = sha256(legacy_value.encode("utf-8")).hexdigest()
    op.execute(
        sa.text(
            "UPDATE plugin_workspaces "
            "SET embedding_config_fingerprint = :fingerprint "
            "WHERE api_key_ciphertext IS NOT NULL"
        ).bindparams(fingerprint=legacy_fingerprint)
    )


def downgrade() -> None:
    op.drop_column("plugin_workspaces", "embedding_config_fingerprint")
    op.alter_column(
        "plugin_workspaces",
        "api_key_ciphertext",
        existing_type=sa.Text(),
        type_=sa.String(length=1024),
        existing_nullable=True,
    )
