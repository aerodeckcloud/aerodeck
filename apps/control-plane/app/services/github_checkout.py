"""Shallow-clone a public GitHub repository into a temporary directory."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.github import canonical_github_repo_url, parse_github_repo
from app.services.docker_policy import CommandRunner, SubprocessCommandRunner, validate_binary


@contextmanager
def github_checkout(
    repo_url: str,
    *,
    git_bin: str = "git",
    runner: CommandRunner | None = None,
) -> Iterator[tuple[str, str, Path]]:
    """Yield ``(canonical_url, repo_name, checkout)`` for one shallow clone."""
    parsed = parse_github_repo(repo_url)
    if parsed is None:
        raise ValueError("repo_url must be a valid GitHub repository URL")
    owner, repo = parsed
    safe_git = validate_binary(git_bin, "git_bin")
    canonical = canonical_github_repo_url(owner, repo)
    if not canonical.startswith("https://github.com/"):
        raise ValueError("only https://github.com repositories may be inspected")
    cmd_runner = runner or SubprocessCommandRunner()
    with tempfile.TemporaryDirectory(prefix="aerodeck-inspect-") as tmp:
        checkout = Path(tmp) / "repo"
        result = cmd_runner.run(
            [
                safe_git,
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "clone",
                "--depth=1",
                "--single-branch",
                "--",
                f"{canonical}.git",
                str(checkout),
            ],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=45,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to inspect repository {owner}/{repo}")
        yield canonical, repo, checkout
