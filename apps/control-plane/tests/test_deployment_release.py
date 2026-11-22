"""Checkout identity and finish time are recorded without reusing SessionLocal (#268)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.db import create_engine
from app.models import Base
from app.models.deployment import Deployment, DeploymentStatus
from app.services.docker_policy import normalize_git_sha
from app.tasks.deployment_status import try_record_deployment_release_sync
from tests.conftest import make_settings

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"


def test_normalize_git_sha_accepts_only_40_hex_chars() -> None:
    assert normalize_git_sha(f"  {SHA.upper()}  ") == SHA
    assert normalize_git_sha(SHA[:7]) is None
    assert normalize_git_sha("g" * 40) is None
    assert normalize_git_sha(None) is None
    assert normalize_git_sha("https://user:token@github.com/acme/repo.git") is None


def test_release_record_keeps_the_first_sha_and_finish_time(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'release.db').as_posix()}"

    async def _seed() -> str:
        engine = create_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            row = Deployment(
                repo_url="https://github.com/mangatohjunior/aerodeck",
                owner="mangatohjunior",
                repo="aerodeck",
                status=DeploymentStatus.running,
                started_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            deployment_id = str(row.id)
        await engine.dispose()
        return deployment_id

    deployment_id = asyncio.run(_seed())
    monkeypatch.setattr(
        "app.tasks.deployment_status.get_settings",
        lambda: make_settings(database_url),
    )

    with caplog.at_level(logging.INFO, logger="aerodeck.services.deployment_terminal"):
        deployment_uuid = uuid.UUID(deployment_id)
        try_record_deployment_release_sync(
            deployment_uuid,
            git_sha=f"{SHA.upper()}\n",
            git_ref="main",
        )
        try_record_deployment_release_sync(
            deployment_uuid,
            git_sha=OTHER_SHA,
            git_ref="https://user:token@github.com/acme/repo.git",
            finished=True,
        )
        try_record_deployment_release_sync(
            deployment_uuid,
            git_sha="not-a-sha",
            git_ref="feature/login",
            finished=True,
        )

    assert SHA not in caplog.text
    assert "token" not in caplog.text

    async def _read() -> Deployment:
        engine = create_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            stored = await session.get(Deployment, uuid.UUID(deployment_id))
            assert stored is not None
        await engine.dispose()
        return stored

    stored = asyncio.run(_read())
    assert stored.git_sha == SHA
    assert stored.git_ref == "main"
    assert stored.finished_at is not None
