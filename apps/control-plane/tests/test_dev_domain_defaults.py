"""The product-zone defaults live in one snippet both Droplet scripts source."""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "scripts"
SNIPPET = ROOT / "dev-domain-defaults.sh"
TEXT = SNIPPET.read_text(encoding="utf-8")


def test_replaces_only_the_previous_dev_literals() -> None:
    assert 'AERODECK_DOCKER_ROUTE_BASE_DOMAIN="dev.aerodeckcloud.com"' in TEXT
    assert 'AERODECK_DNS_RECORD_TARGET="edge.aerodeckcloud.com"' in TEXT
    assert 'AERODECK_DOCKER_ROUTE_BASE_DOMAIN}" == "dev.app.aero-deck.app"' in TEXT
    assert '${AERODECK_DNS_RECORD_TARGET}" == "edge.aero-deck.app"' in TEXT


def test_custom_values_stay_and_previous_defaults_move() -> None:
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to check the domain-default snippet"
    script = "\n".join(
        [
            "set -euo pipefail",
            'AERODECK_DOCKER_ROUTE_BASE_DOMAIN="apps.example.com"',
            'AERODECK_DNS_RECORD_TARGET="edge.example.com"',
            "apply_dev_domain_defaults",
            'printf "%s\\n" "$AERODECK_DOCKER_ROUTE_BASE_DOMAIN"',
            'printf "%s\\n" "$AERODECK_DNS_RECORD_TARGET"',
            "unset AERODECK_DOCKER_ROUTE_BASE_DOMAIN AERODECK_DNS_RECORD_TARGET",
            "apply_dev_domain_defaults",
            'printf "%s\\n" "$AERODECK_DOCKER_ROUTE_BASE_DOMAIN"',
            'printf "%s\\n" "$AERODECK_DNS_RECORD_TARGET"',
            'AERODECK_DOCKER_ROUTE_BASE_DOMAIN="dev.app.aero-deck.app"',
            'AERODECK_DNS_RECORD_TARGET="edge.aero-deck.app"',
            "apply_dev_domain_defaults",
            'printf "%s\\n" "$AERODECK_DOCKER_ROUTE_BASE_DOMAIN"',
            'printf "%s\\n" "$AERODECK_DNS_RECORD_TARGET"',
            "",
        ]
    )
    snippet = SNIPPET.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    result = subprocess.run(
        [bash, "-s"],
        input=snippet + b"\n" + script.encode(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().splitlines() == [
        "apps.example.com",
        "edge.example.com",
        "dev.aerodeckcloud.com",
        "edge.aerodeckcloud.com",
        "dev.aerodeckcloud.com",
        "edge.aerodeckcloud.com",
    ]
