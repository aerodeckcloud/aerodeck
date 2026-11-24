import pytest

from app.services.advisory_source import findings_from_osv
from app.services.manifest_packages import PackageQuery


def test_osv_findings_keep_the_summary_and_drop_the_rest() -> None:
    packages = [PackageQuery("npm", "lodash", "4.17.20")]
    body = {
        "results": [
            {
                "vulns": [
                    {
                        "id": "GHSA-35jh-r3h4-6jhm",
                        "summary": "Prototype pollution in lodash",
                        "details": "Request this URL and send a payload to reproduce the issue.",
                        "database_specific": {"severity": "HIGH"},
                        "references": [{"url": "https://example.invalid/poc"}],
                    }
                ]
            }
        ]
    }
    findings = findings_from_osv(body, packages)
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].package == "lodash"
    assert findings[0].advisory_id == "GHSA-35jh-r3h4-6jhm"
    assert findings[0].summary == "Prototype pollution in lodash"
    assert "payload" not in findings[0].summary
    assert "example.invalid" not in findings[0].model_dump_json()


def test_a_malformed_advisory_response_is_not_a_clear_scan() -> None:
    with pytest.raises(ValueError):
        findings_from_osv({"results": []}, [PackageQuery("npm", "lodash", "1.0.0")])
