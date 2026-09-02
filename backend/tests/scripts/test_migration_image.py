"""imageの実体と承認対象のGit schemaを照合する。"""

from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / ".github" / "scripts"))

image = importlib.import_module("migration_image")


_SHA = "a" * 40
_TREE = "b" * 40
_DIGEST = "sha256:" + "c" * 64
_IMAGE = f"registry.example/vector/backend@{_DIGEST}"
_MIGRATION_PATH = "r1.py"
_MIGRATION_FILES = {_MIGRATION_PATH: ("100644", b"revision = 'r1'\n")}


@pytest.fixture(autouse=True)
def setup_db() -> Iterator[None]:
    """Docker image境界だけを検証するため、DB初期化を行わない。"""
    yield


class FakeRepository:
    def schema(self, release_sha: str) -> SimpleNamespace:
        assert release_sha == _SHA
        return SimpleNamespace(tree_oid=_TREE, head="r1")

    def migration_files(self, release_sha: str) -> dict[str, tuple[str, bytes]]:
        assert release_sha == _SHA
        return dict(_MIGRATION_FILES)


class FakeDocker:
    def __init__(
        self,
        *,
        protocol: object = {"protocol_version": 1},
        files: dict[str, tuple[str, bytes]] | None = None,
        inspection: dict[str, object] | None = None,
    ) -> None:
        self._protocol = protocol
        self._files = dict(_MIGRATION_FILES if files is None else files)
        self._inspection = inspection or {
            "Os": "linux",
            "Architecture": "arm64",
            "RepoDigests": [_IMAGE],
            "Id": _DIGEST,
            "Config": {"Entrypoint": [], "WorkingDir": "/app"},
        }

    def inspect(self, target: str) -> dict[str, object]:
        assert target == _IMAGE
        return dict(self._inspection)

    def protocol(self, target: str) -> object:
        assert target == _IMAGE
        return self._protocol

    def migration_files(self, target: str) -> dict[str, tuple[str, bytes]]:
        assert target == _IMAGE
        return dict(self._files)


def _prepared() -> SimpleNamespace:
    return SimpleNamespace(
        release_sha=_SHA,
        migration_tree_oid=_TREE,
        target_revision="r1",
        result="ready",
        run_id=123,
        run_attempt=2,
    )


@pytest.mark.unit
def test_verified_image_evidence_binds_the_prepared_schema_to_its_digest() -> None:
    evidence = image.verify_image(
        FakeRepository(), _prepared(), _IMAGE, docker=FakeDocker()
    )

    assert evidence == image.ImageEvidence(
        release_sha=_SHA,
        migration_tree_oid=_TREE,
        protocol_version=1,
        image_digest=_DIGEST,
        run_id=123,
        run_attempt=2,
    )
    assert image.ImageEvidence.from_dict(evidence.as_dict()) == evidence


@pytest.mark.parametrize(
    "docker",
    [
        FakeDocker(protocol={"protocol_version": 2}),
        FakeDocker(files={_MIGRATION_PATH: ("100644", b"different\n")}),
        FakeDocker(inspection={"Os": "linux", "Architecture": "amd64"}),
        FakeDocker(
            inspection={
                "Os": "linux",
                "Architecture": "arm64",
                "RepoDigests": [_IMAGE],
                "Id": _DIGEST,
                "Config": {"Entrypoint": ["untrusted-runner"], "WorkingDir": "/app"},
            }
        ),
    ],
)
@pytest.mark.unit
def test_unverified_protocol_schema_or_platform_never_produces_image_evidence(
    docker: FakeDocker,
) -> None:
    with pytest.raises(ValueError):
        image.verify_image(FakeRepository(), _prepared(), _IMAGE, docker=docker)


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol_version": 1},
        {
            "release_sha": _SHA,
            "migration_tree_oid": _TREE,
            "protocol_version": 2,
            "image_digest": _DIGEST,
            "run_id": 123,
            "run_attempt": 2,
        },
        {
            "release_sha": _SHA,
            "migration_tree_oid": _TREE,
            "protocol_version": 1,
            "image_digest": "sha256:bad",
            "run_id": 123,
            "run_attempt": 2,
        },
    ],
)
@pytest.mark.unit
def test_image_evidence_refuses_incomplete_or_untrusted_serialized_data(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        image.ImageEvidence.from_dict(payload)


@pytest.mark.integration
def test_docker_probe_reads_protocol_and_exact_migration_files_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_path = shutil.which("docker")
    git_path = shutil.which("git")
    if docker_path is None or git_path is None:
        pytest.skip("Docker or Git is unavailable")
    if platform.machine().lower() not in {"arm64", "aarch64"}:
        pytest.skip("the production image probe requires a native ARM64 Docker host")
    probe = subprocess.run(  # noqa: S603
        [docker_path, "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    repository_root = tmp_path / "repository"
    context = repository_root / "backend"
    versions = context / "alembic" / "versions"
    scripts = context / "scripts"
    versions.mkdir(parents=True)
    scripts.mkdir()
    migration = versions / "r1.py"
    migration.write_bytes(_MIGRATION_FILES[_MIGRATION_PATH][1])
    migration.chmod(0o755)
    (scripts / "migration_runner.py").write_text(
        "import json\n"
        "import errno\n"
        "import os\n"
        "import socket\n"
        "import sys\n"
        "blocked = {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', "
        "'AWS_SESSION_TOKEN', 'GH_TOKEN'}\n"
        "if blocked.intersection(os.environ) or "
        "sys.argv[1:] != ['--protocol-version']:\n"
        "    raise SystemExit(2)\n"
        "with socket.socket() as connection:\n"
        "    connection.settimeout(1)\n"
        "    try:\n"
        "        connection.connect(('192.0.2.1', 9))\n"
        "    except OSError as error:\n"
        "        if error.errno != errno.ENETUNREACH:\n"
        "            raise SystemExit(3)\n"
        "    else:\n"
        "        raise SystemExit(3)\n"
        "print(json.dumps({'protocol_version': 1}))\n",
        encoding="utf-8",
    )
    (context / "Dockerfile").write_text(
        "FROM --platform=linux/arm64 python:3.13-alpine\n"
        "WORKDIR /app\n"
        "COPY alembic /app/alembic\n"
        "COPY scripts /app/scripts\n",
        encoding="utf-8",
    )
    for variable in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GH_TOKEN",
    ):
        monkeypatch.setenv(variable, "test-probe-not-forwarded")
    for arguments in (
        ["init", "--quiet"],
        ["add", "backend/alembic/versions"],
        [
            "-c",
            "user.name=Migration fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(  # noqa: S603
            [git_path, "-C", str(repository_root), *arguments],
            check=True,
            capture_output=True,
        )
    sha = subprocess.check_output(  # noqa: S603
        [git_path, "-C", str(repository_root), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    repository = image.GitReleaseRepository(repository_root)
    tag = f"vector-migration-probe-test:{uuid.uuid4().hex}"
    try:
        built = subprocess.run(  # noqa: S603
            [
                docker_path,
                "build",
                "--platform",
                "linux/arm64",
                "--provenance=false",
                "--tag",
                tag,
                str(context),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if built.returncode:
            pytest.fail("the local Docker host failed to build the test image")

        docker = image.DockerImageClient()

        assert docker.protocol(tag) == {"protocol_version": 1}
        assert (
            docker.migration_files(tag)
            == repository.migration_files(sha)
            == {"r1.py": ("100755", _MIGRATION_FILES[_MIGRATION_PATH][1])}
        )
    finally:
        subprocess.run(  # noqa: S603
            [docker_path, "image", "rm", "--force", tag],
            capture_output=True,
            check=False,
            timeout=30,
        )
