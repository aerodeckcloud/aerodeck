"""encrypted env var snapshot on succeeded deployments

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-23 09:20:00 UTC

When a provision is marked succeeded, ``env_vars_snapshot`` keeps an encrypted
copy of that environment's variables so a later rollback can restore them
(#273). ``snapshot_names_json`` holds the sorted names in plaintext so list/get
never decrypts the blob. Null means the row predates this story or never
succeeded with a capture.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("env_vars_snapshot", sa.Text(), nullable=True))
        batch.add_column(sa.Column("snapshot_names_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("snapshot_names_json")
        batch.drop_column("env_vars_snapshot")
