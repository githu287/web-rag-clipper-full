"""Add web-body deadlines without changing the published 0011 migration.

Both the GitHub 0011 schema and the earlier local 0011 variant are supported.
Existing web payloads inherit a seven-day deadline from their creation time;
already configured deadlines are preserved. No credentials are read here.
"""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    context = op.get_context()
    offline = context.as_sql
    inspector = None if offline else sa.inspect(op.get_bind())
    columns = set() if offline else {col["name"] for col in inspector.get_columns("ingest_jobs")}
    indexes = set() if offline else {index["name"] for index in inspector.get_indexes("ingest_jobs")}
    if "payload_expires_at" not in columns:
        op.add_column("ingest_jobs", sa.Column("payload_expires_at", sa.DateTime(), nullable=True))
    if "ix_ingest_jobs_payload_expires" not in indexes:
        op.create_index("ix_ingest_jobs_payload_expires", "ingest_jobs", ["payload_expires_at"])

    jobs = sa.table("ingest_jobs", sa.column("payload"), sa.column("job_type"),
                    sa.column("created_at"), sa.column("payload_expires_at"))
    deadline = (sa.func.datetime(jobs.c.created_at, "+604800 seconds")
                if context.dialect.name == "sqlite" else
                sa.func.timestampadd(sa.text("SECOND"), 604800, jobs.c.created_at))
    op.execute(jobs.update().where(
        jobs.c.job_type == "WEB_CLIP", jobs.c.payload.is_not(None),
        jobs.c.payload_expires_at.is_(None),
    ).values(payload_expires_at=deadline))


def downgrade() -> None:
    op.drop_index("ix_ingest_jobs_payload_expires", table_name="ingest_jobs")
    op.drop_column("ingest_jobs", "payload_expires_at")
