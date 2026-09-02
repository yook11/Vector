"""contractに同梱できる変更範囲をCIと本番prepareで共有する。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from scripts.migration_gate import ChangedMigrationDecision, decide_changed_migrations

_SHA = re.compile(r"[0-9a-f]{40}")
_VERSIONS = "backend/alembic/versions/"
_REVISION_PATH = re.compile(r"backend/alembic/versions/[A-Za-z0-9_]+\.py")
_FRONTEND_TEST = re.compile(r"frontend/src/(?:[^/]+/)*[^/]+\.(?:test|spec)\.tsx?")


def is_contract_path_allowed(path: str) -> bool:
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path:
        return False
    return (
        path.startswith(
            (_VERSIONS, "backend/tests/", "frontend/src/test/", "frontend/e2e/")
        )
        or _FRONTEND_TEST.fullmatch(path) is not None
        or path in {"frontend/vitest.setup.client.ts", "frontend/vitest.setup.node.ts"}
        or path.endswith(".md")
    )


class MigrationChangeError(ValueError):
    pass


@dataclass(frozen=True)
class MigrationChangeDecision:
    classification: ChangedMigrationDecision
    rejected_paths: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.classification.decision != "invalid" and not self.rejected_paths


def check_changes(
    repo_root: Path, base_sha: str, head_sha: str
) -> MigrationChangeDecision:
    if not _SHA.fullmatch(base_sha) or not _SHA.fullmatch(head_sha):
        raise MigrationChangeError("invalid_commit_sha")
    git = shutil.which("git")
    if git is None:
        raise MigrationChangeError("git_unavailable")

    def read_git(*args: str) -> bytes:
        try:
            result = subprocess.run(  # noqa: S603
                [git, "-C", str(repo_root), *args],
                capture_output=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            raise MigrationChangeError("git_read_failed") from None
        if result.returncode:
            raise MigrationChangeError("git_read_failed")
        return result.stdout

    for sha in (base_sha, head_sha):
        if read_git("cat-file", "-t", sha).strip() != b"commit":
            raise MigrationChangeError("invalid_commit_object")
    try:
        paths = tuple(
            value.decode("utf-8")
            for value in read_git(
                "diff", "--no-renames", "--name-only", "-z", base_sha, head_sha, "--"
            ).split(b"\0")
            if value
        )
        revisions = tuple(path for path in paths if path.startswith(_VERSIONS))
        with tempfile.TemporaryDirectory(
            prefix="vector-migration-change-"
        ) as directory:
            local_paths = []
            for path in revisions:
                if not _REVISION_PATH.fullmatch(path):
                    raise MigrationChangeError("invalid_revision_path")
                # 削除はHEADに無い。baseの内容で分類し、欠落を黙ってスキップしない。
                entry = read_git("ls-tree", "-z", head_sha, "--", path)
                if not entry:
                    entry = read_git("ls-tree", "-z", base_sha, "--", path)
                if not entry:
                    raise MigrationChangeError("missing_changed_revision")
                metadata, _ = entry.rstrip(b"\0").split(b"\t", 1)
                mode, kind, oid = metadata.split()
                if mode not in {b"100644", b"100755"} or kind != b"blob":
                    raise MigrationChangeError("invalid_revision_file")
                local = Path(directory) / PurePosixPath(path).name
                local.write_text(
                    read_git("cat-file", "blob", oid.decode()).decode("utf-8"),
                    encoding="utf-8",
                )
                local_paths.append(local)
            classification = decide_changed_migrations(local_paths)
    except (OSError, UnicodeError):
        raise MigrationChangeError("migration_source_unreadable") from None
    classification = replace(
        classification,
        revisions=tuple(
            replace(item, path=Path(path))
            for item, path in zip(classification.revisions, revisions, strict=True)
        ),
    )
    rejected = (
        tuple(path for path in paths if not is_contract_path_allowed(path))
        if classification.decision == "manual"
        else ()
    )
    return MigrationChangeDecision(classification, rejected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = check_changes(args.repo_root, args.base_sha, args.head_sha)
    except MigrationChangeError as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                **result.classification.as_dict(),
                "allowed": result.allowed,
                "rejected_paths": list(result.rejected_paths),
            }
        )
    )
    return 0 if result.allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
