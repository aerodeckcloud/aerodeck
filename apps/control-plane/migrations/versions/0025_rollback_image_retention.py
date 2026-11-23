"""keep a bounded number of rollback images per environment

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-23 13:10:00 UTC

``rollback_images_to_keep`` is how many succeeded images an environment retains.
The default is 5 and the value stays between 1 and 20. ``image_pruned_at``
records that a deployment's local tag was removed, so rollback and promote can
refuse it without asking Docker again (#274).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEEP_RANGE = "ck_environments_rollback_images_to_keep_range"


def upgrade() -> None:
    with op.batch_alter_table("environments") as batch:
        batch.add_column(
            sa.Column("rollback_images_to_keep", sa.Integer(), nullable=False, server_default="5")
        )
        batch.create_check_constraint(
            _KEEP_RANGE,
            "rollback_images_to_keep >= 1 AND rollback_images_to_keep <= 20",
        )
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("image_pruned_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("image_pruned_at")
    with op.batch_alter_table("environments") as batch:
        batch.drop_constraint(_KEEP_RANGE, type_="check")
        batch.drop_column("rollback_images_to_keep")
