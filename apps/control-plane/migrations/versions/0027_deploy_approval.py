"""pause auto-deploy and hold production deploys for an owner

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-23 14:40:00 UTC

Existing environments keep both flags false, so current webhooks keep
enqueuing until an owner turns a flag on. New production environments are
set true by the API, not by this default (#276).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("environments") as batch:
        batch.add_column(
            sa.Column(
                "auto_deploy_paused",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "prod_deploy_approval",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("approval_required_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("approved_at")
        batch.drop_column("approval_required_at")
    with op.batch_alter_table("environments") as batch:
        batch.drop_column("prod_deploy_approval")
        batch.drop_column("auto_deploy_paused")
