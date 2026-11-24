"""store a draft's dependency findings and acknowledgement

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-24 13:10:00 UTC

The column holds the page list (severity, package, advisory id, summary)
and who acknowledged it. Upstream advisory bodies are not stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(sa.Column("draft_security", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.drop_column("draft_security")
