"""record the commit, trigger, actor, and finish time on a deployment

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-22 20:40:00 UTC

Older rows stay null. The worker fills ``git_sha`` only after checkout, and
``finished_at`` when the provision reaches a terminal result (#268).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("git_sha", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("git_ref", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("trigger", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("actor_user_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("finished_at")
        batch.drop_column("actor_user_id")
        batch.drop_column("trigger")
        batch.drop_column("git_ref")
        batch.drop_column("git_sha")
