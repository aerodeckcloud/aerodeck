import enum
import uuid

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EnvironmentKind(enum.StrEnum):
    """Mirrors the `environment` variable in packages/iac-engine."""

    dev = "dev"
    staging = "staging"
    prod = "prod"


class DeployOverlap(enum.StrEnum):
    """What a new provision does while another deploy is still queued or running (#271)."""

    wait = "wait"
    override = "override"


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))

    environments: Mapped[list["Environment"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    sandboxes: Mapped[list["Sandbox"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class Environment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_environments_project_id_slug"),
        # Target for deployments composite FK (project_id, environment_id).
        UniqueConstraint("project_id", "id", name="uq_environments_project_id_id"),
        CheckConstraint(
            "rollback_images_to_keep >= 1 AND rollback_images_to_keep <= 20",
            name="rollback_images_to_keep_range",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[EnvironmentKind] = mapped_column(
        Enum(EnvironmentKind, name="environment_kind", native_enum=False, length=16), nullable=False
    )
    region: Mapped[str] = mapped_column(String(16), nullable=False, default="nyc3")
    # wait: queue behind the active deploy. override: cancel it if swap has not started (#271).
    deploy_overlap: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DeployOverlap.wait.value, server_default=DeployOverlap.wait.value
    )
    # How many succeeded deployment images this environment keeps for rollback (#274).
    rollback_images_to_keep: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )

    project: Mapped[Project] = relationship(back_populates="environments")
    routes: Mapped[list["Route"]] = relationship(
        back_populates="environment", cascade="all, delete-orphan", passive_deletes=True
    )
