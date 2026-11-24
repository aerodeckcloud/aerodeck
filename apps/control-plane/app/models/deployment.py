import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKeyConstraint, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Shared with IntegrityError mapping on create (must match migration 0014).
DEPLOYMENT_ENVIRONMENT_FK = "fk_deployments_project_environment"


class DeploymentStatus(enum.StrEnum):
    draft = "draft"
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class DeploymentTrigger(enum.StrEnum):
    """How a deployment row was requested. Stored as text, not a DB enum."""

    manual = "manual"
    webhook = "webhook"
    rollback = "rollback"
    promote = "promote"


class Deployment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A console deployment request. Creating one does not call DigitalOcean."""

    __tablename__ = "deployments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "environment_id"],
            ["environments.project_id", "environments.id"],
            name=DEPLOYMENT_ENVIRONMENT_FK,
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "(project_id IS NULL) = (environment_id IS NULL)",
            # Naming convention expands to ck_deployments_project_environment_pair.
            name="project_environment_pair",
        ),
    )

    repo_url: Mapped[str] = mapped_column(String(512), nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    repo: Mapped[str] = mapped_column(String(100), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    environment_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    # Last Celery provision task id when enqueue succeeded with this deployment linked.
    provision_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Resolved commit after checkout. Null until git rev-parse succeeds (#268).
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    git_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trigger: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set immediately before the live container is removed. Cancel is refused after this (#270).
    swap_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the queue accepts the provision job. Only such a row counts as the
    # active deploy, so a row that was created and never started holds nothing (#271).
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set while this row waits behind another deploy. Status stays queued (#271).
    held_for_deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # What a waiting row needs to start later: git ref, rollback source, addon names,
    # and one-off variables as an encrypted token. Cleared when the row stops waiting.
    provision_replay_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Encrypted copy of the environment's variables when this row succeeded (#273).
    # Same Fernet helper as saved env vars. Null on rows that never captured one.
    env_vars_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sorted JSON name list for the snapshot. Plaintext so reads never decrypt (#273).
    snapshot_names_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when this deployment's local image tag was removed by retention (#274).
    image_pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set while a production deploy is waiting for an owner. Status stays queued (#276).
    approval_required_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last confirmed step. Plaintext so a resume link does not decrypt variables (#343).
    draft_step: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Encrypted blueprint, addon selection, and variable values. Cleared once the row leaves draft.
    draft_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus, name="deployment_status", native_enum=False, length=16),
        nullable=False,
        default=DeploymentStatus.running,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"Deployment(id={self.id!s})"
