"""project deployment notification channels

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-22 19:15:00 UTC

Store encrypted per-project Slack, Discord, and generic webhook notification
channels for deployment lifecycle alerts (#259).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False, server_default="slack"),
        sa.Column("encrypted_url", sa.Text(), nullable=False),
        sa.Column("url_hint", sa.String(length=128), nullable=False),
        sa.Column("notify_on_started", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notify_on_succeeded", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_on_failed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_channels"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", name="uq_notification_channels_project_id"),
    )
    op.create_index("ix_notification_channels_project_id", "notification_channels", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_channels_project_id", table_name="notification_channels")
    op.drop_table("notification_channels")
