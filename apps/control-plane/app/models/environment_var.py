"""Encrypted per-environment runtime variables (#248)."""

import uuid

from sqlalchemy import Boolean, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EnvironmentVariable(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "environment_variables"
    __table_args__ = (
        UniqueConstraint("environment_id", "key", name="uq_environment_variables_env_key"),
        ForeignKeyConstraint(
            ["project_id", "environment_id"],
            ["environments.project_id", "environments.id"],
            name="fk_environment_variables_project_environment",
            ondelete="CASCADE",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
