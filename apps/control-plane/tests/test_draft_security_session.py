"""The dependency scan must not hold a pooled connection while it clones or calls OSV."""

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.models.deployment import DeploymentStatus
from app.schemas.draft_security import DraftSecurityRecord, SecurityFinding
from app.services import deployment_draft as draft_module
from app.services.deployment_draft import DeploymentDraftService
from app.services.manifest_packages import ManifestRead, PackageQuery

_ACTOR = uuid.uuid4()


class _Deployments:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.row = SimpleNamespace(
            id=uuid.uuid4(),
            repo_url="https://github.com/acme/demo",
            status=DeploymentStatus.draft,
            actor_user_id=_ACTOR,
            draft_security=None,
        )

    async def get(self, deployment_id: uuid.UUID) -> SimpleNamespace:
        self.events.append("get")
        return self.row

    async def commit(self) -> None:
        self.events.append("commit")


class _Manifests:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def read(self, repo_url: str) -> ManifestRead:
        assert self._events[-1] == "commit"
        self._events.append("read")
        return ManifestRead(found=False, packages=(), unpinned=0)


class _SlowAdvisories:
    async def query(self, packages: list[PackageQuery]) -> list[object]:
        await asyncio.sleep(1)
        return []


def _service(deployments: _Deployments, packages: object, advisories: object) -> DeploymentDraftService:
    return DeploymentDraftService("k" * 32, deployments, None, None, packages, advisories)  # type: ignore[arg-type]


async def test_the_scan_releases_the_session_before_reading_the_repository() -> None:
    deployments = _Deployments()
    service = _service(deployments, _Manifests(deployments.events), _SlowAdvisories())
    record = await service.scan_security(deployments.row.id, _ACTOR)
    assert record.outcome == "no_manifest"
    assert deployments.events == ["get", "commit", "read", "get", "commit"]
    assert deployments.row.draft_security is not None


async def test_a_rescan_keeps_an_acknowledgement_saved_while_it_ran() -> None:
    deployments = _Deployments()
    finding = SecurityFinding(
        severity="high",
        package="lodash",
        advisory_id="GHSA-test",
        summary="Prototype pollution in lodash",
    )

    class _AckDuringRead:
        def read(self, repo_url: str) -> ManifestRead:
            saved = DraftSecurityRecord(
                outcome="findings",
                findings=[finding],
                acknowledgement="findings",
                acknowledged_at=datetime.now(UTC),
                acknowledged_by=_ACTOR,
                scanned_at=datetime.now(UTC),
            )
            deployments.row.draft_security = saved.model_dump_json()
            return ManifestRead(
                found=True,
                packages=(PackageQuery("npm", "lodash", "4.17.21"),),
                unpinned=0,
            )

    class _SameFinding:
        async def query(self, packages: list[PackageQuery]) -> list[SecurityFinding]:
            return [finding]

    service = _service(deployments, _AckDuringRead(), _SameFinding())
    record = await service.scan_security(deployments.row.id, _ACTOR)
    assert record.outcome == "findings"
    assert record.acknowledgement == "findings"
    assert record.acknowledged_by == _ACTOR


async def test_an_advisory_deadline_is_an_incomplete_scan(monkeypatch) -> None:
    monkeypatch.setattr(draft_module, "_ADVISORY_DEADLINE_SECONDS", 0.05)

    class _Pinned:
        def read(self, repo_url: str) -> ManifestRead:
            return ManifestRead(
                found=True,
                packages=(PackageQuery("npm", "lodash", "4.17.21"),),
                unpinned=0,
            )

    service = _service(_Deployments(), _Pinned(), _SlowAdvisories())
    record = await service._scan_record("https://github.com/acme/demo")
    assert record.outcome == "incomplete"
