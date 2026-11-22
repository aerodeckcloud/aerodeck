"""Project membership and role-based access control (#256)."""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProjectRole(enum.StrEnum):
    owner = "owner"
    developer = "developer"
    viewer = "viewer"


class ProjectMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("console_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[ProjectRole] = mapped_column(
        Enum(ProjectRole, name="project_role", native_enum=False, length=16),
        nullable=False,
        default=ProjectRole.viewer,
    )
