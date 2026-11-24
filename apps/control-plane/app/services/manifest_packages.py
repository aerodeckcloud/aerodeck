"""Read pinned dependencies from the manifests the inspector already recognizes (#345)."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_MAX_BYTES = 512 * 1024
_EXACT_VERSION = re.compile(r"^v?(\d+\.\d+\.\d+)$", re.IGNORECASE)
_EXACT_EQUAL = re.compile(r"^==\s*(\d+\.\d+\.\d+)$")
_PY_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)")
_SKIP_VERSION = ("workspace:", "file:", "git+", "git:", "github:", "http://", "https://", "link:")


@dataclass(frozen=True, slots=True)
class PackageQuery:
    ecosystem: str
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ManifestRead:
    found: bool
    packages: tuple[PackageQuery, ...]
    unpinned: int


def read_manifests(checkout: Path) -> ManifestRead:
    """Root manifests only. The repository's install scripts are not executed."""
    packages: list[PackageQuery] = []
    unpinned = 0
    found = False
    for reader, names in (
        (_npm, ("package.json",)),
        (_requirements, ("requirements.txt",)),
        (_pyproject, ("pyproject.toml",)),
        (_go_mod, ("go.mod",)),
    ):
        for name in names:
            path = checkout / name
            if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_BYTES:
                continue
            found = True
            pinned, missed = reader(path.read_text(encoding="utf-8", errors="replace"))
            packages.extend(pinned)
            unpinned += missed
    unique = _dedupe(packages)
    return ManifestRead(found=found, packages=tuple(unique[:40]), unpinned=unpinned + max(0, len(unique) - 40))


class PackageSource(Protocol):
    def read(self, repo_url: str) -> ManifestRead:
        """Return the root manifests, or raise when the repository cannot be read."""


class GitPackageSource:
    """Clone the draft's repository and read its root manifests."""

    def __init__(self, git_bin: str = "git") -> None:
        self._git_bin = git_bin

    def read(self, repo_url: str) -> ManifestRead:
        from app.services.github_checkout import github_checkout

        with github_checkout(repo_url, git_bin=self._git_bin) as (_, _, checkout):
            return read_manifests(checkout)


def _dedupe(packages: list[PackageQuery]) -> list[PackageQuery]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[PackageQuery] = []
    for package in packages:
        key = (package.ecosystem, package.name, package.version)
        if key in seen:
            continue
        seen.add(key)
        unique.append(package)
    return unique


def _pinned_version(raw: str) -> str | None:
    """An exact release, such as ``4.17.21``, ``==4.2.1``, or Go ``v1.2.3``.

    A range (``^``, ``~``, ``>=``, and the rest) is not the version that will be installed.
    """
    text = raw.strip()
    if not text or any(token in text for token in _SKIP_VERSION):
        return None
    match = _EXACT_VERSION.match(text) or _EXACT_EQUAL.match(text)
    return match.group(1) if match else None


def _npm(text: str) -> tuple[list[PackageQuery], int]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], 1
    if not isinstance(data, dict):
        return [], 1
    pinned: list[PackageQuery] = []
    missed = 0
    for field in ("dependencies", "optionalDependencies"):
        block = data.get(field)
        if not isinstance(block, dict):
            continue
        for name, raw in block.items():
            if not isinstance(name, str) or not isinstance(raw, str):
                missed += 1
                continue
            version = _pinned_version(raw)
            if version is None:
                missed += 1
                continue
            pinned.append(PackageQuery("npm", name.strip(), version))
    return pinned, missed


def _requirements(text: str) -> tuple[list[PackageQuery], int]:
    pinned: list[PackageQuery] = []
    missed = 0
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _PY_NAME.match(line)
        if match is None:
            missed += 1
            continue
        version = _pinned_version(line[match.end() :])
        if version is None:
            missed += 1
            continue
        pinned.append(PackageQuery("PyPI", match.group(1), version))
    return pinned, missed


def _pyproject(text: str) -> tuple[list[PackageQuery], int]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [], 1
    pinned: list[PackageQuery] = []
    missed = 0
    project = data.get("project")
    if isinstance(project, dict) and isinstance(project.get("dependencies"), list):
        for item in project["dependencies"]:
            if not isinstance(item, str):
                missed += 1
                continue
            added, skipped = _requirements(item)
            pinned.extend(added)
            missed += skipped
    poetry = data.get("tool")
    if isinstance(poetry, dict):
        poetry = poetry.get("poetry")
    if isinstance(poetry, dict) and isinstance(poetry.get("dependencies"), dict):
        for name, raw in poetry["dependencies"].items():
            if name == "python" or not isinstance(name, str):
                continue
            version_text = raw if isinstance(raw, str) else raw.get("version") if isinstance(raw, dict) else None
            if not isinstance(version_text, str):
                missed += 1
                continue
            version = _pinned_version(version_text)
            if version is None:
                missed += 1
                continue
            pinned.append(PackageQuery("PyPI", name, version))
    return pinned, missed


def _go_mod(text: str) -> tuple[list[PackageQuery], int]:
    pinned: list[PackageQuery] = []
    missed = 0
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line or line.startswith(("module ", "go ")):
            continue
        parts = line.removeprefix("require ").split()
        if len(parts) < 2 or parts[0] in {"require", "(", ")"}:
            if parts and parts[0] not in {"require", "(", ")"}:
                missed += 1
            continue
        version = _pinned_version(parts[1])
        if version is None:
            missed += 1
            continue
        pinned.append(PackageQuery("Go", parts[0], version))
    return pinned, missed
