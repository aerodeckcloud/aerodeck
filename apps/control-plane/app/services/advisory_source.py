"""Query a public advisory database. The stored result is the page list, not the response (#345)."""

from __future__ import annotations

from typing import Protocol

import httpx

from app.schemas.draft_security import SecurityFinding
from app.services.manifest_packages import PackageQuery

_SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}
_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}


class AdvisorySource(Protocol):
    async def query(self, packages: list[PackageQuery]) -> list[SecurityFinding]:
        """Return findings, or raise when the source cannot be read."""


class OsvAdvisorySource:
    def __init__(self, base_url: str = "https://api.osv.dev") -> None:
        self._base_url = base_url.rstrip("/")

    async def query(self, packages: list[PackageQuery]) -> list[SecurityFinding]:
        if not packages:
            return []
        payload = {
            "queries": [
                {
                    "version": package.version,
                    "package": {"name": package.name, "ecosystem": package.ecosystem},
                }
                for package in packages
            ]
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{self._base_url}/v1/querybatch", json=payload)
            response.raise_for_status()
            body = response.json()
        return findings_from_osv(body, packages)


def findings_from_osv(body: object, packages: list[PackageQuery]) -> list[SecurityFinding]:
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ValueError("advisory response was not a result list")
    results = body["results"]
    if len(results) != len(packages):
        raise ValueError("advisory response did not match the query")
    found: list[SecurityFinding] = []
    seen: set[tuple[str, str]] = set()
    for package, result in zip(packages, results, strict=True):
        if not isinstance(result, dict):
            raise ValueError("advisory result was not an object")
        vulns = result.get("vulns") or []
        if not isinstance(vulns, list):
            raise ValueError("advisory result was not a list")
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            advisory_id = vuln.get("id")
            if not isinstance(advisory_id, str) or not advisory_id.strip():
                continue
            key = (package.name, advisory_id.strip())
            if key in seen:
                continue
            seen.add(key)
            summary = vuln.get("summary")
            text = " ".join(summary.split()) if isinstance(summary, str) else ""
            if not text:
                text = f"Public advisory {advisory_id.strip()}."
            found.append(
                SecurityFinding(
                    severity=_severity(vuln),
                    package=package.name[:200],
                    advisory_id=advisory_id.strip()[:64],
                    summary=text[:160],
                )
            )
            if len(found) >= 30:
                return sorted(found, key=lambda item: _RANK[item.severity])
    return sorted(found, key=lambda item: _RANK[item.severity])


def _severity(vuln: dict[str, object]) -> str:
    specific = vuln.get("database_specific")
    if isinstance(specific, dict):
        raw = specific.get("severity")
        if isinstance(raw, str) and raw.upper() in _SEVERITY:
            return _SEVERITY[raw.upper()]
    return "unknown"
