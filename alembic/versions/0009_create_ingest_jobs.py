"""create persistent asynchronous ingest jobs

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="QUEUED", nullable=False),
        sa.Column("stage", sa.String(length=32), server_default="QUEUED", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["plugin_id"],
            ["plugin_workspaces.plugin_id"],
            name="fk_ingest_jobs_plugin_id_plugin_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_ingest_jobs_document_id_documents",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingest_jobs_plugin_status",
        "ingest_jobs",
        ["plugin_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_ingest_jobs_document_id",
        "ingest_jobs",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    # MySQL may select these indexes to enforce the two foreign keys, so
    # dropping either index first raises error 1553. Dropping the table lets
    # the database remove its foreign keys and indexes in one valid operation.
    op.drop_table("ingest_jobs")
