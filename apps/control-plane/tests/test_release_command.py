"""Release command argv parsing and the worker timeout cap (#275)."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.docker_policy import parse_release_command
from app.services.provisioning import PermanentJobError


def test_parse_release_command_splits_an_executable_and_arguments() -> None:
    assert parse_release_command("python manage.py migrate") == ["python", "manage.py", "migrate"]
    assert parse_release_command("  ") == []
    assert parse_release_command(None) == []


@pytest.mark.parametrize(
    "command",
    ["echo a && echo b", "echo a | wc", "echo a; echo b", "echo `id`", "echo a\necho b", "'unterminated"],
)
def test_parse_release_command_rejects_a_shell_script(command: str) -> None:
    with pytest.raises(PermanentJobError, match="not a shell script"):
        parse_release_command(command)


def test_release_command_timeout_defaults_to_five_minutes_and_caps_at_fifteen() -> None:
    settings = Settings(auth_signing_key="a" * 32)
    assert settings.release_command_timeout_seconds == 300
    assert Settings(auth_signing_key="a" * 32, release_command_timeout_seconds=900).release_command_timeout_seconds == 900
    with pytest.raises(ValidationError):
        Settings(auth_signing_key="a" * 32, release_command_timeout_seconds=901)
