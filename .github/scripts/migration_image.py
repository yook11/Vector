"""資格情報を渡さない実image検証を、ECS起動前の必須条件にする。"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from migration_prepare import GitReleaseRepository, PreparedMigration, require_sha

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@(sha256:[0-9a-f]{64})")


class ImageVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class ImageEvidence:
    release_sha: str
    migration_tree_oid: str
    protocol_version: int
    image_digest: str
    run_id: int
    run_attempt: int

    def __post_init__(self) -> None:
        require_sha(self.release_sha)
        require_sha(self.migration_tree_oid)
        if (
            type(self.protocol_version) is not int
            or self.protocol_version != 1
            or not isinstance(self.image_digest, str)
            or not _DIGEST.fullmatch(self.image_digest)
            or type(self.run_id) is not int
            or self.run_id < 1
            or type(self.run_attempt) is not int
            or self.run_attempt < 1
        ):
            raise ImageVerificationError("invalid_image_evidence")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> ImageEvidence:
        try:
            if not isinstance(value, dict):
                raise ValueError
            return cls(**value)
        except (TypeError, ValueError):
            raise ImageVerificationError("invalid_image_evidence") from None


class DockerImageClient:
    def __init__(self) -> None:
        docker = shutil.which("docker")
        if docker is None:
            raise ImageVerificationError("docker_unavailable")
        self._docker = docker

    def _run(self, *args: str) -> bytes:
        try:
            result = subprocess.run(  # noqa: S603
                [self._docker, *args],
                capture_output=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            raise ImageVerificationError("docker_verification_failed") from None
        if result.returncode:
            raise ImageVerificationError("docker_verification_failed")
        return result.stdout

    def inspect(self, image: str) -> Mapping[str, object]:
        value = json.loads(self._run("image", "inspect", image))
        if (
            not isinstance(value, list)
            or len(value) != 1
            or not isinstance(value[0], dict)
        ):
            raise ImageVerificationError("invalid_image_inspection")
        return value[0]

    def protocol(self, image: str) -> object:
        output = self._run(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--entrypoint",
            "python",
            image,
            "-m",
            "scripts.migration_runner",
            "--protocol-version",
        )
        return json.loads(output)

    def migration_files(self, image: str) -> dict[str, tuple[str, bytes]]:
        container = (
            self._run("create", "--network", "none", "--entrypoint", "python", image)
            .decode()
            .strip()
        )
        if re.fullmatch(r"[0-9a-f]{64}", container) is None:
            raise ImageVerificationError("invalid_probe_container")
        try:
            archive = self._run("cp", f"{container}:/app/alembic/versions/.", "-")
            return _archive_files(archive)
        finally:
            self._run("rm", "--volumes", container)


def _archive_files(archive: bytes) -> dict[str, tuple[str, bytes]]:
    files: dict[str, tuple[str, bytes]] = {}
    if len(archive) > 32 * 1024 * 1024:
        raise ImageVerificationError("migration_archive_too_large")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ImageVerificationError("invalid_migration_archive_path")
            if member.isdir():
                continue
            key = path.as_posix()
            if key in files:
                raise ImageVerificationError("duplicate_migration_archive_path")
            if member.issym():
                files[key] = ("120000", member.linkname.encode())
            elif member.isfile():
                reader = stream.extractfile(member)
                if reader is None:
                    raise ImageVerificationError("unreadable_migration_archive_file")
                files[key] = (
                    "100755" if member.mode & 0o111 else "100644",
                    reader.read(),
                )
            else:
                raise ImageVerificationError("unsupported_migration_archive_file")
    return files


def verify_image(
    repository: GitReleaseRepository,
    prepared: PreparedMigration,
    image: str,
    *,
    docker: DockerImageClient | None = None,
) -> ImageEvidence:
    try:
        match = _IMAGE.fullmatch(image)
        if match is None or prepared.result != "ready":
            raise ImageVerificationError("invalid_image_reference")
        client = docker if docker is not None else DockerImageClient()
        inspection = client.inspect(image)
        image_config = inspection.get("Config")
        if (
            inspection.get("Os") != "linux"
            or inspection.get("Architecture") != "arm64"
            or image not in inspection.get("RepoDigests", [])
            or not isinstance(inspection.get("Id"), str)
            or not _DIGEST.fullmatch(inspection["Id"])
            or not isinstance(image_config, Mapping)
            or image_config.get("Entrypoint") not in (None, [])
            or image_config.get("WorkingDir") != "/app"
        ):
            raise ImageVerificationError("image_platform_or_digest_mismatch")
        version = client.protocol(image)
        if (
            not isinstance(version, dict)
            or set(version) != {"protocol_version"}
            or type(version["protocol_version"]) is not int
            or version["protocol_version"] != 1
        ):
            raise ImageVerificationError("unsupported_image_protocol")
        schema = repository.schema(prepared.release_sha)
        if (
            schema.tree_oid != prepared.migration_tree_oid
            or schema.head != prepared.target_revision
        ):
            raise ImageVerificationError("prepared_schema_mismatch")
        if client.migration_files(image) != repository.migration_files(
            prepared.release_sha
        ):
            raise ImageVerificationError("image_migration_tree_mismatch")
        return ImageEvidence(
            prepared.release_sha,
            schema.tree_oid,
            1,
            match.group(1),
            prepared.run_id,
            prepared.run_attempt,
        )
    except ImageVerificationError:
        raise
    except Exception:
        raise ImageVerificationError("image_verification_failed") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        prepared = PreparedMigration.from_dict(json.loads(args.prepared.read_text()))
        evidence = verify_image(
            GitReleaseRepository(args.repo_root), prepared, args.image
        )
        args.output.write_text(json.dumps(evidence.as_dict()) + "\n")
        return 0
    except Exception:
        print("::error::migration_image_verification_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
