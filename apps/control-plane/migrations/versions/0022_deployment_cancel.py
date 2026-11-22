"""cancel a deployment before the live container swap

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-22 21:25:00 UTC

``deployment_status`` is VARCHAR(16). ``cancelled`` fits, same as ``failed``
in 0013. ``swap_started_at`` is set immediately before cutover (#270).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("swap_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE deployments SET status = 'failed' WHERE status = 'cancelled'")
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("swap_started_at")
