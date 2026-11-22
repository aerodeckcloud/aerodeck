"""GitHub push webhook configuration per environment (#249)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EnvironmentWebhook(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "environment_webhooks"
    __table_args__ = (
        UniqueConstraint("environment_id", name="uq_environment_webhooks_environment_id"),
        ForeignKeyConstraint(
            ["project_id", "environment_id"],
            ["environments.project_id", "environments.id"],
            name="fk_environment_webhooks_project_environment",
            ondelete="CASCADE",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repo_url: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    branch: Mapped[str] = mapped_column(String(128), nullable=False, default="main")
    addons_json: Mapped[str | None] = mapped_column(String(256))
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_delivery_id: Mapped[str | None] = mapped_column(String(128))
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
