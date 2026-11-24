import asyncio
import json

import httpx
import pytest

from app.services.advisory_source import OsvAdvisorySource, advisory_ids_from_batch, finding_from_advisory
from app.services.manifest_packages import PackageQuery

_PACKAGES = [PackageQuery("npm", "lodash", "4.17.20")]
_ADVISORY_ID = "GHSA-35jh-r3h4-6jhm"


def test_querybatch_returns_ids_without_a_summary() -> None:
    body = {"results": [{"vulns": [{"id": _ADVISORY_ID, "modified": "2021-05-01T00:00:00Z"}]}]}
    assert advisory_ids_from_batch(body, _PACKAGES) == [("lodash", _ADVISORY_ID)]


def test_an_advisory_detail_keeps_the_summary_and_drops_the_rest() -> None:
    finding = finding_from_advisory(
        "lodash",
        {
            "id": _ADVISORY_ID,
            "summary": "Prototype pollution in lodash",
            "details": "Request this URL and send a payload to reproduce the issue.",
            "database_specific": {"severity": "HIGH"},
            "references": [{"url": "https://example.invalid/poc"}],
        },
    )
    assert finding.severity == "high"
    assert finding.package == "lodash"
    assert finding.advisory_id == _ADVISORY_ID
    assert finding.summary == "Prototype pollution in lodash"
    assert "payload" not in finding.summary
    assert "example.invalid" not in finding.model_dump_json()


def test_a_malformed_advisory_response_is_not_a_clear_scan() -> None:
    with pytest.raises(ValueError):
        advisory_ids_from_batch({"results": []}, _PACKAGES)


async def test_osv_query_loads_each_advisory_after_the_batch() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/v1/querybatch":
            query = json.loads(request.content)["queries"][0]
            assert query["package"] == {"name": "lodash", "ecosystem": "npm"}
            assert query["version"] == "4.17.20"
            return httpx.Response(
                200,
                json={"results": [{"vulns": [{"id": _ADVISORY_ID, "modified": "2021-05-01T00:00:00Z"}]}]},
            )
        if request.url.path == f"/v1/vulns/{_ADVISORY_ID}":
            return httpx.Response(
                200,
                json={
                    "id": _ADVISORY_ID,
                    "summary": "Prototype pollution in lodash",
                    "details": "Request this URL and send a payload to reproduce the issue.",
                    "database_specific": {"severity": "HIGH"},
                    "references": [{"url": "https://example.invalid/poc"}],
                },
            )
        return httpx.Response(404)

    findings = await OsvAdvisorySource(transport=httpx.MockTransport(handler)).query(_PACKAGES)
    assert requested == ["/v1/querybatch", f"/v1/vulns/{_ADVISORY_ID}"]
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].summary == "Prototype pollution in lodash"
    assert "payload" not in findings[0].model_dump_json()
    assert "example.invalid" not in findings[0].model_dump_json()


async def test_advisory_detail_fetches_stay_bounded() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        if request.url.path == "/v1/querybatch":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"vulns": [{"id": f"OSV-2021-{index}", "modified": "2021-01-01T00:00:00Z"} for index in range(6)]}
                    ]
                },
            )
        if request.url.path.startswith("/v1/vulns/"):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            advisory_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={"id": advisory_id, "summary": "A public advisory.", "database_specific": {"severity": "LOW"}},
            )
        return httpx.Response(404)

    findings = await OsvAdvisorySource(transport=httpx.MockTransport(handler)).query(_PACKAGES)
    assert len(findings) == 6
    assert peak == 5


async def test_a_later_critical_advisory_is_kept_ahead_of_earlier_lows() -> None:
    low_ids = [f"LOW-{index}" for index in range(30)]
    critical_id = "CRIT-1"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/querybatch":
            vulns = [{"id": advisory_id, "modified": "2021-01-01T00:00:00Z"} for advisory_id in [*low_ids, critical_id]]
            return httpx.Response(200, json={"results": [{"vulns": vulns}]})
        if request.url.path.startswith("/v1/vulns/"):
            advisory_id = request.url.path.rsplit("/", 1)[-1]
            severity = "CRITICAL" if advisory_id == critical_id else "LOW"
            return httpx.Response(
                200,
                json={"id": advisory_id, "summary": "A public advisory.", "database_specific": {"severity": severity}},
            )
        return httpx.Response(404)

    findings = await OsvAdvisorySource(transport=httpx.MockTransport(handler)).query(_PACKAGES)
    assert len(findings) == 30
    assert findings[0].advisory_id == critical_id
    assert findings[0].severity == "critical"
    assert low_ids[-1] not in {item.advisory_id for item in findings}
