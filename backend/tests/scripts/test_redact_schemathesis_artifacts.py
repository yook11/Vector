"""Schemathesis artifact のfail-closed redaction契約テスト。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import redact_schemathesis_artifacts as redaction  # noqa: E402

_RAW_JWT = ".".join(
    (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEifQ",
        "ZGVmZW5zZS1pbi1kZXB0aC1zaWduYXR1cmU",
    )
)
_JWT_SHAPED_VALUE = ".".join(("a" * 10, "b" * 10, "c" * 10))


def test_redact_artifact_file_removes_authorization_and_bare_jwt(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "cassette.yaml"
    artifact.write_text(
        f"Authorization: Bearer {_RAW_JWT}\ntoken: {_RAW_JWT}\n",
        encoding="utf-8",
    )

    processed = redaction.redact_artifact_paths([artifact])

    assert (processed, artifact.read_text(encoding="utf-8")) == (
        1,
        "Authorization: ***\ntoken: eyJ***\n",
    )


def test_redact_artifact_directory_processes_nested_files(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "report"
    nested_dir = artifact_dir / "nested"
    nested_dir.mkdir(parents=True)
    first = artifact_dir / "junit.xml"
    second = nested_dir / "cassette.yaml"
    first.write_text("<testsuite />\n", encoding="utf-8")
    second.write_text(
        "database=postgresql://vector:password@localhost/vector\n",
        encoding="utf-8",
    )

    processed = redaction.redact_artifact_paths([artifact_dir])

    assert (
        processed,
        first.read_text(encoding="utf-8"),
        second.read_text(encoding="utf-8"),
    ) == (2, "<testsuite />\n", "database=postgresql://***@localhost/vector\n")


def test_redact_artifact_paths_counts_zero_byte_regular_file(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "empty.log"
    artifact.touch()

    processed = redaction.redact_artifact_paths([artifact])

    assert (processed, artifact.read_bytes()) == (1, b"")


def test_redact_artifact_paths_rejects_missing_root_before_rewriting(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "backend.log"
    original = f"Authorization: Bearer {_RAW_JWT}\n"
    artifact.write_text(original, encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        redaction.redact_artifact_paths([artifact, tmp_path / "missing-report"])

    assert artifact.read_text(encoding="utf-8") == original


def test_redact_artifact_paths_rejects_empty_directory(
    tmp_path: Path,
) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    with pytest.raises(FileNotFoundError):
        redaction.redact_artifact_paths([empty_dir])


def test_redact_artifact_paths_rejects_dangling_root_symlink(
    tmp_path: Path,
) -> None:
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing-target")

    with pytest.raises(ValueError):
        redaction.redact_artifact_paths([dangling])


def test_redact_artifact_paths_rejects_nonregular_root(tmp_path: Path) -> None:
    fifo = tmp_path / "artifact.fifo"
    os.mkfifo(fifo)

    with pytest.raises(ValueError):
        redaction.redact_artifact_paths([fifo])


def test_redact_artifact_directory_rejects_descendant_symlink_before_rewriting(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "report"
    artifact_dir.mkdir()
    redactable = artifact_dir / "a-cassette.yaml"
    original = f"Authorization: Bearer {_RAW_JWT}\n"
    redactable.write_text(original, encoding="utf-8")
    target = tmp_path / "outside.log"
    target.write_text("backend ready\n", encoding="utf-8")
    (artifact_dir / "z-linked.log").symlink_to(target)

    with pytest.raises(ValueError):
        redaction.redact_artifact_paths([artifact_dir])

    assert redactable.read_text(encoding="utf-8") == original


def test_redact_artifact_directory_rejects_descendant_nonregular_file(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "report"
    artifact_dir.mkdir()
    redactable = artifact_dir / "a-cassette.yaml"
    original = f"Authorization: Bearer {_RAW_JWT}\n"
    redactable.write_text(original, encoding="utf-8")
    os.mkfifo(artifact_dir / "z-artifact.fifo")

    with pytest.raises(ValueError):
        redaction.redact_artifact_paths([artifact_dir])

    assert redactable.read_text(encoding="utf-8") == original


def test_redact_artifact_paths_rejects_residual_jwt(tmp_path: Path) -> None:
    artifact = tmp_path / "cassette.yaml"
    artifact.write_text(_JWT_SHAPED_VALUE, encoding="utf-8")

    with pytest.raises(ValueError):
        redaction.redact_artifact_paths([artifact])


def test_redact_artifact_paths_validates_every_file_before_rewriting(
    tmp_path: Path,
) -> None:
    redactable = tmp_path / "a-cassette.yaml"
    invalid_utf8 = tmp_path / "z-backend.log"
    original = f"Authorization: Bearer {_RAW_JWT}\n"
    redactable.write_text(original, encoding="utf-8")
    invalid_utf8.write_bytes(b"backend ready\n\xff")

    with pytest.raises(UnicodeDecodeError):
        redaction.redact_artifact_paths([redactable, invalid_utf8])

    assert redactable.read_text(encoding="utf-8") == original
