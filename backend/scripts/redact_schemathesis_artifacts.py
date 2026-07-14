"""Schemathesis artifactをupload前に伏字化する。"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

from app.shared.security.redaction import redact_secrets

_JWT_SHAPE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"(?![A-Za-z0-9_-])"
)


def _artifact_files(paths: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"symlink artifact path is not allowed: {path}")
        if not path.exists():
            raise FileNotFoundError(f"artifact path does not exist: {path}")
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(path.rglob("*"))
        else:
            raise ValueError(
                f"artifact path is not a regular file or directory: {path}"
            )

        root_files = 0
        for candidate in candidates:
            if candidate.is_symlink():
                raise ValueError(f"symlink artifact path is not allowed: {candidate}")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise ValueError(f"artifact is not a regular file: {candidate}")
            root_files += 1
            if candidate in seen:
                continue
            seen.add(candidate)
            files.append(candidate)
        if root_files == 0:
            raise FileNotFoundError(f"artifact path contains no files: {path}")
    if not files:
        raise FileNotFoundError("no artifact paths were provided")
    return files


def redact_artifact_paths(paths: Sequence[Path]) -> int:
    """全fileを検証できた場合だけ伏字化した内容を書き戻す。"""
    redacted_files: dict[Path, str] = {}
    for path in _artifact_files(paths):
        redacted = redact_secrets(path.read_text(encoding="utf-8"))
        if _JWT_SHAPE_RE.search(redacted):
            raise ValueError(f"JWT-shaped value remains after redaction: {path}")
        redacted_files[path] = redacted

    for path, redacted in redacted_files.items():
        path.write_text(redacted, encoding="utf-8")
    return len(redacted_files)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    count = redact_artifact_paths(args.paths)
    print(f"redacted {count} artifact file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
