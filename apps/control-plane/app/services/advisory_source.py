"""Query a public advisory database. The stored result is the page list, not the response (#345)."""

from __future__ import annotations

import asyncio
from typing import Protocol
from urllib.parse import quote

import httpx

from app.schemas.draft_security import SecurityFinding
from app.services.manifest_packages import PackageQuery

_SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}
_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
_MAX_STORED_FINDINGS = 30
# Severity is only on the detail record, so the stored page is chosen after those fetches.
_MAX_DETAIL_IDS = 120
_DETAIL_CONCURRENCY = 5


class AdvisorySource(Protocol):
    async def query(self, packages: list[PackageQuery]) -> list[SecurityFinding]:
        """Return findings, or raise when the source cannot be read."""


class OsvAdvisorySource:
    def __init__(
        self,
        base_url: str = "https://api.osv.dev",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def query(self, packages: list[PackageQuery]) -> list[SecurityFinding]:
        if not packages:
            return []
        async with httpx.AsyncClient(
            timeout=10.0, transport=self._transport, base_url=self._base_url
        ) as client:
            response = await client.post(
                "/v1/querybatch",
                json={
                    "queries": [
                        {
                            "version": package.version,
                            "package": {"name": package.name, "ecosystem": package.ecosystem},
                        }
                        for package in packages
                    ]
                },
            )
            response.raise_for_status()
            pairs = advisory_ids_from_batch(response.json(), packages)
            details = await _advisory_details(client, [advisory_id for _, advisory_id in pairs])
        findings: list[SecurityFinding] = []
        for (package_name, _), detail in zip(pairs, details, strict=True):
            detail.raise_for_status()
            findings.append(finding_from_advisory(package_name, detail.json()))
        ranked = sorted(findings, key=lambda item: _RANK[item.severity])
        return ranked[:_MAX_STORED_FINDINGS]


async def _advisory_details(client: httpx.AsyncClient, advisory_ids: list[str]) -> list[httpx.Response]:
    semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)

    async def load(advisory_id: str) -> httpx.Response:
        async with semaphore:
            return await client.get(f"/v1/vulns/{quote(advisory_id, safe='')}")

    return list(await asyncio.gather(*(load(advisory_id) for advisory_id in advisory_ids)))


def advisory_ids_from_batch(body: object, packages: list[PackageQuery]) -> list[tuple[str, str]]:
    """Ids from a querybatch body. That response has no summary or severity."""
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ValueError("advisory response was not a result list")
    results = body["results"]
    if len(results) != len(packages):
        raise ValueError("advisory response did not match the query")
    found: list[tuple[str, str]] = []
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
            found.append((package.name[:200], advisory_id.strip()))
            if len(found) >= _MAX_DETAIL_IDS:
                return found
    return found


def finding_from_advisory(package_name: str, vuln: object) -> SecurityFinding:
    """One stored row from GET /v1/vulns/{id}. Details and references are not copied."""
    if not isinstance(vuln, dict):
        raise ValueError("advisory detail was not an object")
    advisory_id = vuln.get("id")
    if not isinstance(advisory_id, str) or not advisory_id.strip():
        raise ValueError("advisory detail had no id")
    summary = vuln.get("summary")
    text = " ".join(summary.split()) if isinstance(summary, str) else ""
    if not text:
        text = f"Public advisory {advisory_id.strip()}."
    return SecurityFinding(
        severity=_severity(vuln),
        package=package_name[:200],
        advisory_id=advisory_id.strip()[:64],
        summary=text[:160],
    )


def _severity(vuln: dict[str, object]) -> str:
    specific = vuln.get("database_specific")
    if isinstance(specific, dict):
        raw = specific.get("severity")
        if isinstance(raw, str) and raw.upper() in _SEVERITY:
            return _SEVERITY[raw.upper()]
    return "unknown"
