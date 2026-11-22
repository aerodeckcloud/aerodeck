"""environment github push webhooks

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-22 16:05:00 UTC

Store per-environment GitHub push webhook configuration and encrypted signing
secrets for automatic blue/green redeploys (#249).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "environment_webhooks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repo_url", sa.String(length=512), nullable=False),
        sa.Column("branch", sa.String(length=128), nullable=False, server_default="main"),
        sa.Column("addons_json", sa.String(length=256), nullable=True),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_delivery_id", sa.String(length=128), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_environment_webhooks"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["project_id", "environment_id"],
            ["environments.project_id", "environments.id"],
            name="fk_environment_webhooks_project_environment",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("environment_id", name="uq_environment_webhooks_environment_id"),
    )
    op.create_index("ix_environment_webhooks_project_id", "environment_webhooks", ["project_id"])
    op.create_index(
        "ix_environment_webhooks_environment_id", "environment_webhooks", ["environment_id"]
    )
    op.create_index("ix_environment_webhooks_repo_url", "environment_webhooks", ["repo_url"])


def downgrade() -> None:
    op.drop_index("ix_environment_webhooks_repo_url", table_name="environment_webhooks")
    op.drop_index("ix_environment_webhooks_environment_id", table_name="environment_webhooks")
    op.drop_index("ix_environment_webhooks_project_id", table_name="environment_webhooks")
    op.drop_table("environment_webhooks")
