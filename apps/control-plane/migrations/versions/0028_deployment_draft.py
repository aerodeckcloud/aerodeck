"""store a deployment draft without queueing provision

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-23 22:40:00 UTC

``deployment_status`` is VARCHAR(16). ``draft`` fits, same as ``failed``.
The plan column holds ciphertext. Downgrade drops the columns after any
draft row is cancelled so it cannot be launched with no plan.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("draft_step", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("draft_plan", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute(
        "UPDATE deployments SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP "
        "WHERE status = 'draft'"
    )
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("draft_plan")
        batch.drop_column("draft_step")
