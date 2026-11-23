"""Release command argv parsing and the worker timeout cap (#275)."""

import sys
import time

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.docker_policy import CommandTimeoutError, SubprocessCommandRunner, parse_release_command


def test_parse_release_command_splits_an_executable_and_arguments() -> None:
    assert parse_release_command("python manage.py migrate") == ["python", "manage.py", "migrate"]
    assert parse_release_command("  ") == []
    assert parse_release_command(None) == []


@pytest.mark.parametrize(
    "command",
    ["echo a && echo b", "echo a | wc", "echo a; echo b", "echo `id`", "echo a\necho b", "'unterminated"],
)
def test_parse_release_command_rejects_a_shell_script(command: str) -> None:
    with pytest.raises(ValueError, match="not a shell script"):
        parse_release_command(command)


def test_parse_release_command_treats_a_backslash_as_an_escape() -> None:
    """The API is authoritative. A trailing backslash is invalid; an escaped space joins tokens."""
    assert parse_release_command("echo a\\ b") == ["echo", "a b"]
    with pytest.raises(ValueError):
        parse_release_command("echo a\\")


def test_streaming_runner_times_out_while_stdout_stays_open() -> None:
    """A process that prints and then hangs must hit the deadline before the pipe closes."""
    lines: list[str] = []
    started = time.monotonic()
    with pytest.raises(CommandTimeoutError, match="timed out after 2s"):
        SubprocessCommandRunner().run(
            [sys.executable, "-c", "import time; print('still going', flush=True); time.sleep(30)"],
            timeout=2,
            on_line=lines.append,
        )
    assert time.monotonic() - started < 10
    assert lines == ["still going"]


def test_streaming_runner_returns_output_that_finishes_in_time() -> None:
    lines: list[str] = []
    result = SubprocessCommandRunner().run(
        [sys.executable, "-c", "print('ok')"],
        timeout=10,
        on_line=lines.append,
    )
    assert result.returncode == 0
    assert lines == ["ok"]
    assert result.stdout == "ok"


def test_release_command_timeout_defaults_to_five_minutes_and_caps_at_fifteen() -> None:
    settings = Settings(auth_signing_key="a" * 32)
    assert settings.release_command_timeout_seconds == 300
    assert Settings(auth_signing_key="a" * 32, release_command_timeout_seconds=900).release_command_timeout_seconds == 900
    with pytest.raises(ValidationError):
        Settings(auth_signing_key="a" * 32, release_command_timeout_seconds=901)
