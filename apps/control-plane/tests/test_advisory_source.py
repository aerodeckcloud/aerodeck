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
