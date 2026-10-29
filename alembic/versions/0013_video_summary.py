"""Independent encrypted video configuration and durable summary queue."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("video_model_configs",
        sa.Column("plugin_id", sa.String(64), sa.ForeignKey("plugin_workspaces.plugin_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False), sa.Column("nonce", sa.String(32), nullable=False))
    op.create_table("video_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plugin_id", sa.String(64), sa.ForeignKey("plugin_workspaces.plugin_id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(180), nullable=False), sa.Column("source_url", sa.String(2048)),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("stage", sa.String(80), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease", sa.String(36)), sa.Column("lease_until", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("result", sa.Text().with_variant(LONGTEXT(), "mysql")), sa.Column("error", sa.String(300)))
    for column in ("plugin_id", "status", "expires_at"):
        op.create_index("ix_video_jobs_" + column, "video_jobs", [column])


def downgrade():
    op.drop_table("video_jobs")
    op.drop_table("video_model_configs")
