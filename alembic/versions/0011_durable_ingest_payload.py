"""persist ingest delivery payload with the job

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingest_jobs", sa.Column("payload", sa.JSON(), nullable=True))
    op.create_index("ix_ingest_jobs_status_created", "ingest_jobs", ["status", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_ingest_jobs_status_created", table_name="ingest_jobs")
    op.drop_column("ingest_jobs", "payload")
