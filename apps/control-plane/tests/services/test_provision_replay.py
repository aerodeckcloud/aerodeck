"""Stored provision payloads survive a round trip and bad ones are reported (#271)."""

import uuid

import pytest

from app.services.provision_replay import ProvisionReplay

LOGGER = "aerodeck.services.provision_replay"


def test_round_trip_keeps_every_field() -> None:
    replay = ProvisionReplay(
        git_ref="a" * 40,
        rollback_from_deployment_id=uuid.uuid4(),
        addons=["redis"],
        sealed_env_vars="sealed-token",
    )

    assert ProvisionReplay.from_json(replay.to_json()) == replay


def test_missing_payload_is_empty_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger=LOGGER):
        assert ProvisionReplay.from_json(None) == ProvisionReplay()

    assert caplog.text == ""


@pytest.mark.parametrize(
    ("raw", "reason"),
    [("{not json sealed-token", "invalid_json"), ('["sealed-token"]', "not_an_object")],
)
def test_unreadable_payload_is_empty_and_logged_without_its_content(
    raw: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger=LOGGER):
        assert ProvisionReplay.from_json(raw) == ProvisionReplay()

    assert f"event=provision_replay_unreadable reason={reason}" in caplog.text
    assert "sealed-token" not in caplog.text
