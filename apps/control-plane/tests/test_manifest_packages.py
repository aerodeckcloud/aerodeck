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


def test_dev_dependency_pins_are_part_of_the_scan(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"devDependencies": {"lodash": "4.17.21"}}',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            (
                "[project.optional-dependencies]",
                'dev = ["httpx==0.27.0"]',
                "",
                "[dependency-groups]",
                'lint = ["ruff==0.6.0"]',
                "",
                "[tool.poetry.dev-dependencies]",
                'certifi = "==2024.2.2"',
                "",
                "[tool.poetry.group.dev.dependencies]",
                'pytest = "==8.2.0"',
            )
        ),
        encoding="utf-8",
    )

    read = read_manifests(tmp_path)
    assert read.found is True
    assert read.unpinned == 0
    found = {(item.ecosystem, item.name, item.version) for item in read.packages}
    assert found == {
        ("npm", "lodash", "4.17.21"),
        ("PyPI", "httpx", "0.27.0"),
        ("PyPI", "ruff", "0.6.0"),
        ("PyPI", "certifi", "2024.2.2"),
        ("PyPI", "pytest", "8.2.0"),
    }


def test_go_mod_directives_other_than_require_are_not_unpinned(tmp_path) -> None:
    (tmp_path / "go.mod").write_text(
        "\n".join(
            (
                "module example.com/demo",
                "",
                "go 1.22",
                "",
                "toolchain go1.22.5",
                "",
                "require (",
                "\tgithub.com/pkg/errors v0.9.1",
                ")",
                "",
                "replace (",
                "\tgithub.com/pkg/errors => github.com/pkg/errors v0.9.0",
                ")",
                "",
                "exclude github.com/pkg/errors v0.8.0",
            )
        ),
        encoding="utf-8",
    )

    read = read_manifests(tmp_path)
    assert read.found is True
    assert read.unpinned == 0
    found = {(item.ecosystem, item.name, item.version) for item in read.packages}
    assert found == {("Go", "github.com/pkg/errors", "0.9.1")}


def test_a_dockerfile_without_a_manifest_is_not_a_scan(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    read = read_manifests(tmp_path)
    assert read.found is False
    assert read.packages == ()
