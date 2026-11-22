"""persistent encrypted environment variables

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-22 15:50:00 UTC

Store per-environment runtime configuration encrypted at rest (#248).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "environment_variables",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.PrimaryKeyConstraint("id", name="pk_environment_variables"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["project_id", "environment_id"],
            ["environments.project_id", "environments.id"],
            name="fk_environment_variables_project_environment",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("environment_id", "key", name="uq_environment_variables_env_key"),
    )
    op.create_index("ix_environment_variables_project_id", "environment_variables", ["project_id"])
    op.create_index(
        "ix_environment_variables_environment_id", "environment_variables", ["environment_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_environment_variables_environment_id", table_name="environment_variables")
    op.drop_index("ix_environment_variables_project_id", table_name="environment_variables")
    op.drop_table("environment_variables")
