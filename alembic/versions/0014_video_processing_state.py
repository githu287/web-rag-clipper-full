"""Persist fenced video checkpoints and confirmed/unknown cloud usage."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("video_jobs", sa.Column("processing_state", sa.Text().with_variant(LONGTEXT(), "mysql"), nullable=True))


def downgrade():
    op.drop_column("video_jobs", "processing_state")
