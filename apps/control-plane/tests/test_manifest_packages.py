from app.services.manifest_packages import read_manifests


def test_exact_pins_are_read_and_ranges_stay_unpinned(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"lodash": "4.17.21", "async": "^3.2.4", "local": "workspace:*"}}',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text(
        "Django==4.2.1\nrequests\nflask>=2.0.0\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.11"\nhttpx = "^0.27.0"\ncertifi = "==2024.2.2"\n',
        encoding="utf-8",
    )
    (tmp_path / "go.mod").write_text(
        "module example.com/demo\n\ngo 1.22\n\nrequire github.com/pkg/errors v0.9.1\n",
        encoding="utf-8",
    )

    read = read_manifests(tmp_path)
    assert read.found is True
    assert read.unpinned == 5
    found = {(item.ecosystem, item.name, item.version) for item in read.packages}
    assert found == {
        ("npm", "lodash", "4.17.21"),
        ("PyPI", "Django", "4.2.1"),
        ("PyPI", "certifi", "2024.2.2"),
        ("Go", "github.com/pkg/errors", "0.9.1"),
    }


def test_a_dockerfile_without_a_manifest_is_not_a_scan(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    read = read_manifests(tmp_path)
    assert read.found is False
    assert read.packages == ()
