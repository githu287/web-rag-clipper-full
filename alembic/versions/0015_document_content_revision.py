"""Fence retrieved evidence against completed document updates."""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column(
        "content_revision", sa.BigInteger(), nullable=False, server_default="0",
    ))


def downgrade():
    op.drop_column("documents", "content_revision")
