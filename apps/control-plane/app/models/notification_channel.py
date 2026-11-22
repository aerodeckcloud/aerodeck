"""Per-project deployment notification channels (Slack, Discord, generic webhook) (#259)."""

import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NotificationProvider(enum.StrEnum):
    slack = "slack"
    discord = "discord"
    webhook = "webhook"


class NotificationChannel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_channels"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_notification_channels_project_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[NotificationProvider] = mapped_column(
        Enum(NotificationProvider, name="notification_provider", native_enum=False, length=16),
        nullable=False,
        default=NotificationProvider.slack,
    )
    encrypted_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hint: Mapped[str] = mapped_column(String(128), nullable=False)
    notify_on_started: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notify_on_succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_on_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
