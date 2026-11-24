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


def _version_table(
    block: object, *, ecosystem: str, skip: frozenset[str] = frozenset()
) -> tuple[list[PackageQuery], int]:
    """Name-to-version tables. A missing table was not written; a present one we cannot read counts."""
    if block is None:
        return [], 0
    if not isinstance(block, dict):
        return [], 1
    pinned: list[PackageQuery] = []
    missed = 0
    for name, raw in block.items():
        if not isinstance(name, str) or name in skip:
            continue
        version_text = raw if isinstance(raw, str) else raw.get("version") if isinstance(raw, dict) else None
        if not isinstance(version_text, str):
            missed += 1
            continue
        version = _pinned_version(version_text)
        if version is None:
            missed += 1
            continue
        pinned.append(PackageQuery(ecosystem, name.strip(), version))
    return pinned, missed


def _requirement_list(items: object) -> tuple[list[PackageQuery], int]:
    if items is None:
        return [], 0
    if not isinstance(items, list):
        return [], 1
    pinned: list[PackageQuery] = []
    missed = 0
    for item in items:
        if not isinstance(item, str):
            missed += 1
            continue
        added, skipped = _requirements(item)
        pinned.extend(added)
        missed += skipped
    return pinned, missed


def _requirement_groups(groups: object) -> tuple[list[PackageQuery], int]:
    if groups is None:
        return [], 0
    if not isinstance(groups, dict):
        return [], 1
    pinned: list[PackageQuery] = []
    missed = 0
    for items in groups.values():
        added, skipped = _requirement_list(items)
        pinned.extend(added)
        missed += skipped
    return pinned, missed


def _npm(text: str) -> tuple[list[PackageQuery], int]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], 1
    if not isinstance(data, dict):
        return [], 1
    pinned: list[PackageQuery] = []
    missed = 0
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        added, skipped = _version_table(data.get(field), ecosystem="npm")
        pinned.extend(added)
        missed += skipped
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
    if not isinstance(data, dict):
        return [], 1
    pinned: list[PackageQuery] = []
    missed = 0
    project = data.get("project")
    if isinstance(project, dict):
        added, skipped = _requirement_list(project.get("dependencies"))
        pinned.extend(added)
        missed += skipped
        added, skipped = _requirement_groups(project.get("optional-dependencies"))
        pinned.extend(added)
        missed += skipped
    added, skipped = _requirement_groups(data.get("dependency-groups"))
    pinned.extend(added)
    missed += skipped
    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if not isinstance(poetry, dict):
        return pinned, missed
    for field in ("dependencies", "dev-dependencies"):
        added, skipped = _version_table(poetry.get(field), ecosystem="PyPI", skip=frozenset({"python"}))
        pinned.extend(added)
        missed += skipped
    groups = poetry.get("group")
    if isinstance(groups, dict):
        for group in groups.values():
            block = group.get("dependencies") if isinstance(group, dict) else None
            if group is not None and not isinstance(group, dict):
                missed += 1
                continue
            added, skipped = _version_table(block, ecosystem="PyPI", skip=frozenset({"python"}))
            pinned.extend(added)
            missed += skipped
    elif groups is not None:
        missed += 1
    return pinned, missed


def _go_require(path: str, version: str) -> tuple[PackageQuery | None, int]:
    pinned = _pinned_version(version)
    if pinned is None:
        return None, 1
    return PackageQuery("Go", path, pinned), 0


def _go_mod(text: str) -> tuple[list[PackageQuery], int]:
    """Require lines only. toolchain, replace, exclude, and retract are not dependencies."""
    pinned: list[PackageQuery] = []
    missed = 0
    in_require = False
    in_other = False
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if in_other:
            if line == ")":
                in_other = False
            continue
        if not line:
            continue
        if in_require:
            if line == ")":
                in_require = False
                continue
            parts = line.split()
            if len(parts) < 2:
                missed += 1
                continue
            package, skipped = _go_require(parts[0], parts[1])
            if package is not None:
                pinned.append(package)
            missed += skipped
            continue
        if line.startswith("require") and line.endswith("("):
            in_require = True
            continue
        if line.endswith("("):
            in_other = True
            continue
        if not line.startswith("require "):
            continue
        parts = line.removeprefix("require ").split()
        if len(parts) < 2:
            missed += 1
            continue
        package, skipped = _go_require(parts[0], parts[1])
        if package is not None:
            pinned.append(package)
        missed += skipped
    return pinned, missed
