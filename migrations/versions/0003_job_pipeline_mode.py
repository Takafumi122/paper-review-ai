"""Persist the requested pipeline mode independently for every job."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_jobs") as batch:
        batch.add_column(
            sa.Column("pipeline_mode", sa.String(), nullable=False, server_default="phase0_fake")
        )


def downgrade() -> None:
    with op.batch_alter_table("analysis_jobs") as batch:
        batch.drop_column("pipeline_mode")
