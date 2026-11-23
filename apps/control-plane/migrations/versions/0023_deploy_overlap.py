"""one deployment at a time, with wait or override

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-22 21:45:00 UTC

``deploy_overlap`` defaults to wait for existing environments. ``dispatched_at``
marks the row the queue accepted; only such a row counts as the active deploy,
so rows from before this revision never hold a new one. A waiting deployment
stays ``queued`` and points at the active row. ``provision_replay_json`` keeps
what it needs to start later and is cleared when it stops waiting (#271).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("environments") as batch:
        batch.add_column(
            sa.Column("deploy_overlap", sa.String(length=16), nullable=False, server_default="wait")
        )
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("held_for_deployment_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("provision_replay_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("provision_replay_json")
        batch.drop_column("held_for_deployment_id")
        batch.drop_column("dispatched_at")
    with op.batch_alter_table("environments") as batch:
        batch.drop_column("deploy_overlap")
