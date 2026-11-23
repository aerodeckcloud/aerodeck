"""optional release command before traffic cutover

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-23 13:50:00 UTC

``release_command`` is an executable and its arguments. The worker runs it
inside the new container after the health check and before Traefik moves
traffic. Null means the swap behaves as it did before this column (#275).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("environments") as batch:
        batch.add_column(sa.Column("release_command", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("environments") as batch:
        batch.drop_column("release_command")
