"""publish-console-route.py must not print the client secret and renews fake certs."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish-console-route.py"
TEXT = SCRIPT.read_text(encoding="utf-8")


def _load_module():
    spec = importlib.util.spec_from_file_location("publish_console_route", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publishes_the_dev_console_without_echoing_the_secret() -> None:
    assert 'HOSTNAME = "dev.aerodeckcloud.com"' in TEXT
    assert 'HOSTNAME = "dev.app.aero-deck.app"' not in TEXT
    assert 'BACKEND_HOST = os.environ.get("AERODECK_CONTROL_PRIVATE_IP") or "10.41.0.2"' in TEXT
    assert "/etc/aerodeck/dashboard.env" in TEXT
    assert "certificate" in TEXT
    assert "/issue" in TEXT
    assert "/renew" in TEXT
    printed = TEXT[TEXT.index('print(f"hostname'):]
    assert "SECRET" not in printed
    assert "access_token" not in printed


def test_environment_variable_defaults() -> None:
    mod = _load_module()
    assert mod.HOSTNAME == "dev.aerodeckcloud.com"
    assert mod.BACKEND_HOST == "10.41.0.2"
    assert mod.API == "http://127.0.0.1:8000"
    assert str(mod.ENV_FILE).replace("\\", "/") == "/etc/aerodeck/dashboard.env" 


def test_publish_adds_the_new_host_and_leaves_the_old_route(monkeypatch) -> None:
    mod = _load_module()
    calls: list[tuple[str, str]] = []
    posts: list[dict] = []

    def fake_call(method: str, path: str, body: dict | None = None, token: str | None = None):
        _ = token
        calls.append((method, path))
        if method == "GET":
            return {
                "items": [
                    {
                        "id": "old-route",
                        "hostname": "dev.app.aero-deck.app",
                        "backend_host": mod.BACKEND_HOST,
                    }
                ]
            }
        assert method == "POST"
        assert body is not None
        posts.append(body)
        return {"id": "new-route", "hostname": body["hostname"]}

    monkeypatch.setattr(mod, "call", fake_call)
    created = mod.ensure_route("project-1", "env-1", "token")
    assert created["hostname"] == "dev.aerodeckcloud.com"
    assert not any(method in {"DELETE", "PATCH"} and "old-route" in path for method, path in calls)
    assert posts == [
        {
            "environment_id": "env-1",
            "hostname": "dev.aerodeckcloud.com",
            "path_prefix": "/",
            "backend_host": mod.BACKEND_HOST,
            "backend_port": 3000,
        }
    ]


def test_second_publish_reuses_the_console_project_and_route(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "dashboard.env"
    env_file.write_text(
        "CONTROL_PLANE_CLIENT_ID=console-id\nCONTROL_PLANE_CLIENT_SECRET=console-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AERODECK_DASHBOARD_ENV", str(env_file))
    mod = _load_module()
    posts: list[str] = []

    def fake_call(method: str, path: str, body: dict | None = None, token: str | None = None):
        _ = (body, token)
        if method == "POST" and path == "/api/v1/auth/token":
            return {"access_token": "tok"}
        if method == "GET" and path.startswith("/api/v1/projects?"):
            return {"items": [{"id": "project-1", "slug": "console"}]}
        if method == "GET" and path.endswith("/environments?limit=200"):
            return {"items": [{"id": "env-1", "slug": "dev"}]}
        if method == "GET" and path.endswith("/routes?limit=200"):
            return {
                "items": [
                    {
                        "id": "route-1",
                        "hostname": mod.HOSTNAME,
                        "backend_host": mod.BACKEND_HOST,
                    }
                ]
            }
        if method == "GET" and path.endswith("/certificate"):
            return {"status": "issued", "issuer": "Let's Encrypt"}
        if method == "POST":
            posts.append(path)
            return {"id": "created"}
        raise AssertionError((method, path))

    monkeypatch.setattr(mod, "call", fake_call)
    mod.main([])
    assert posts == []


def test_should_renew_when_fake_or_staging_or_forced() -> None:
    mod = _load_module()
    assert mod.should_renew_certificate({"status": "issued", "issuer": "AeroDeck Fake CA"}, False) is True
    assert mod.should_renew_certificate({"status": "issued", "issuer": "(STAGING) Let's Encrypt"}, False) is True
    assert mod.should_renew_certificate({"status": "issued", "issuer": "Let's Encrypt"}, False) is False
    assert mod.should_renew_certificate({"status": "issued", "issuer": "Let's Encrypt"}, True) is True
