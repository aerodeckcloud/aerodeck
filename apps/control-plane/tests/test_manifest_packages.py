from app.services.manifest_packages import read_manifests


def test_pinned_manifests_are_read_and_ranges_keep_the_written_version(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"lodash": "^4.17.21", "local": "workspace:*"}}',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("Django==4.2.1\nrequests\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text(
        "module example.com/demo\n\ngo 1.22\n\nrequire github.com/pkg/errors v0.9.1\n",
        encoding="utf-8",
    )

    read = read_manifests(tmp_path)
    assert read.found is True
    assert read.unpinned == 2
    found = {(item.ecosystem, item.name, item.version) for item in read.packages}
    assert ("npm", "lodash", "4.17.21") in found
    assert ("PyPI", "Django", "4.2.1") in found
    assert ("Go", "github.com/pkg/errors", "0.9.1") in found


def test_a_dockerfile_without_a_manifest_is_not_a_scan(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    read = read_manifests(tmp_path)
    assert read.found is False
    assert read.packages == ()
