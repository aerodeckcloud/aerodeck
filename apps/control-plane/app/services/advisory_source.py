"""Query a public advisory database. The stored result is the page list, not the response (#345)."""

from __future__ import annotations

import asyncio
import math
from typing import Protocol
from urllib.parse import quote

import httpx

from app.schemas.draft_security import SecurityFinding
from app.services.manifest_packages import PackageQuery

_SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}
_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
_MAX_STORED_FINDINGS = 30
# Every id in the batch is fetched. Severity is on the detail, so the page is chosen after that.
_DETAIL_CONCURRENCY = 5
_V3_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_V3_AC = {"L": 0.77, "H": 0.44}
_V3_UI = {"N": 0.85, "R": 0.62}
_V3_CIA = {"N": 0.0, "L": 0.22, "H": 0.56}
_V3_PR = {"U": {"N": 0.85, "L": 0.62, "H": 0.27}, "C": {"N": 0.85, "L": 0.68, "H": 0.5}}
_V2_AV = {"L": 0.395, "A": 0.646, "N": 1.0}
_V2_AC = {"H": 0.35, "M": 0.61, "L": 0.71}
_V2_AU = {"M": 0.45, "S": 0.56, "N": 0.704}
_V2_CIA = {"N": 0.0, "P": 0.275, "C": 0.66}


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
    """CVSS on ``severity[]`` wins. ``database_specific.severity`` is the fallback."""
    ranked = _severity_from_array(vuln.get("severity"))
    if ranked is not None:
        return ranked
    specific = vuln.get("database_specific")
    if isinstance(specific, dict):
        label = _named_severity(specific.get("severity"))
        if label is not None:
            return label
    return "unknown"


def _severity_from_array(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    best: str | None = None
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _label_from_score(item.get("type"), item.get("score"))
        if label is None:
            continue
        if best is None or _RANK[label] < _RANK[best]:
            best = label
    return best


def _label_from_score(kind: object, score: object) -> str | None:
    if not isinstance(score, str) or not score.strip():
        return None
    text = score.strip()
    named = _named_severity(text)
    if named is not None and not text.upper().startswith("CVSS:"):
        return named
    numeric = _numeric_score(text)
    if numeric is not None:
        return _band(numeric, critical=_cvss_has_critical(kind, text))
    base = _cvss_base(kind, text)
    if base is None:
        return None
    return _band(base, critical=_cvss_has_critical(kind, text))


def _named_severity(value: object) -> str | None:
    if isinstance(value, str) and value.strip().upper() in _SEVERITY:
        return _SEVERITY[value.strip().upper()]
    return None


def _numeric_score(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    if 0 < value <= 10:
        return value
    return None


def _cvss_has_critical(kind: object, text: str) -> bool:
    if isinstance(kind, str) and kind.upper() == "CVSS_V2":
        return False
    return not text.upper().startswith("CVSS:2")


def _cvss_base(kind: object, text: str) -> float | None:
    metrics = _metrics(text)
    family = text.upper()
    version = kind.upper() if isinstance(kind, str) else ""
    if version == "CVSS_V2" or family.startswith("CVSS:2"):
        return _v2_base(metrics)
    if version == "CVSS_V4" or family.startswith("CVSS:4"):
        return None
    if version == "CVSS_V3" or family.startswith("CVSS:3") or "AV" in metrics:
        return _v3_base(metrics)
    return None


def _metrics(score: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in score.split("/"):
        key, separator, raw = part.partition(":")
        if separator and not key.upper().startswith("CVSS"):
            found[key.upper()] = raw.upper()
    return found


def _v3_base(metrics: dict[str, str]) -> float | None:
    try:
        scope = metrics["S"]
        impact = 1 - (
            (1 - _V3_CIA[metrics["C"]]) * (1 - _V3_CIA[metrics["I"]]) * (1 - _V3_CIA[metrics["A"]])
        )
        exploitability = (
            8.22 * _V3_AV[metrics["AV"]] * _V3_AC[metrics["AC"]] * _V3_PR[scope][metrics["PR"]] * _V3_UI[metrics["UI"]]
        )
    except KeyError:
        return None
    if scope == "U":
        impact_sub = 6.42 * impact
    elif scope == "C":
        impact_sub = 7.52 * (impact - 0.029) - 3.25 * (impact - 0.02) ** 15
    else:
        return None
    if impact_sub <= 0:
        return 0.0
    raw = impact_sub + exploitability
    if scope == "C":
        raw *= 1.08
    return _roundup(min(raw, 10))


def _v2_base(metrics: dict[str, str]) -> float | None:
    try:
        impact = 10.41 * (
            1
            - (1 - _V2_CIA[metrics["C"]]) * (1 - _V2_CIA[metrics["I"]]) * (1 - _V2_CIA[metrics["A"]])
        )
        exploitability = 20 * _V2_AV[metrics["AV"]] * _V2_AC[metrics["AC"]] * _V2_AU[metrics["AU"]]
    except KeyError:
        return None
    if impact <= 0:
        return 0.0
    return _round1(((0.6 * impact) + (0.4 * exploitability) - 1.5) * 1.176)


def _band(score: float, *, critical: bool) -> str | None:
    if score >= 9.0 and critical:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0:
        return "low"
    return None


def _roundup(value: float) -> float:
    scaled = int(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000
    return (scaled // 10000 + 1) / 10


def _round1(value: float) -> float:
    return math.floor(value * 10 + 0.5) / 10
