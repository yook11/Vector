"""承認前の期待値だけを作り、対象commitのPythonコードは実行しない。"""

from __future__ import annotations

import argparse
import ast
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from alembic.script.revision import Revision, RevisionMap  # noqa: E402
from migration_ledger import (  # noqa: E402
    Baseline,
    GitHubApi,
    GitHubCli,
    LedgerRecord,
    LedgerSnapshot,
    MigrationLedger,
)

from scripts.migration_change_gate import is_contract_path_allowed  # noqa: E402
from scripts.migration_gate import decide_changed_migrations  # noqa: E402

_SHA = re.compile(r"[0-9a-f]{40}")
_REVISION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}")
_VERSIONS = "backend/alembic/versions"
_MODES = {"expand", "contract", "verify"}


class PreparationError(ValueError):
    pass


def require_sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise PreparationError("invalid_release_sha")
    return value


def require_revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _REVISION.fullmatch(value)
        or value in {"base", "head", "heads"}
    ):
        raise PreparationError("invalid_revision")
    return value


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


@dataclass(frozen=True)
class PreparedMigration:
    release_sha: str
    mode: str
    target_revision: str
    migration_tree_oid: str
    expected_start_revision: str | None
    baseline: LedgerSnapshot
    contract_parent_sha: str | None
    run_id: int
    run_attempt: int
    result: str
    pending_revisions: tuple[str, ...] | None
    classifications: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        require_sha(self.release_sha)
        require_sha(self.migration_tree_oid)
        require_revision(self.target_revision)
        if self.expected_start_revision is not None:
            require_revision(self.expected_start_revision)
        if self.contract_parent_sha is not None:
            require_sha(self.contract_parent_sha)
        if (
            self.mode not in _MODES
            or not _positive(self.run_id)
            or not _positive(self.run_attempt)
            or self.result not in {"ready", "no_changes"}
            or not isinstance(self.baseline, LedgerSnapshot)
            or self.baseline.baseline is None
            or (self.mode != "verify" and self.expected_start_revision is None)
            or (self.mode == "contract") != (self.contract_parent_sha is not None)
        ):
            raise PreparationError("invalid_prepared_migration")
        if self.pending_revisions is not None:
            for revision in self.pending_revisions:
                require_revision(revision)

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["pending_revisions"] = (
            list(self.pending_revisions) if self.pending_revisions is not None else None
        )
        value["classifications"] = list(self.classifications)
        return value

    @classmethod
    def from_dict(cls, value: object) -> PreparedMigration:
        try:
            if not isinstance(value, dict):
                raise ValueError
            fields = dict(value)
            snapshot = dict(fields["baseline"])
            raw_baseline = snapshot["baseline"]
            if not isinstance(raw_baseline, dict):
                raise ValueError
            baseline = Baseline(**raw_baseline)
            if not (
                baseline.deployment_id is None or _positive(baseline.deployment_id)
            ) or not (
                baseline.status_id is None
                or baseline.deployment_id is not None
                and _positive(baseline.status_id)
            ):
                raise ValueError
            snapshot["baseline"] = baseline
            if snapshot["record"] is not None:
                snapshot["record"] = LedgerRecord.from_payload(snapshot["record"])
            if snapshot["state"] not in {"available", "unavailable", "missing"}:
                raise ValueError
            if snapshot["state"] == "available" and (
                snapshot["record"] is None
                or snapshot["latest_status"] != "success"
                or baseline.deployment_id is None
                or baseline.status_id is None
            ):
                raise ValueError
            if snapshot["state"] == "missing" and (
                baseline != Baseline(None, None)
                or snapshot["record"] is not None
                or snapshot["latest_status"] is not None
            ):
                raise ValueError
            if snapshot["state"] == "unavailable" and baseline.deployment_id is None:
                raise ValueError
            fields["baseline"] = LedgerSnapshot(**snapshot)
            pending = fields["pending_revisions"]
            if pending is not None and not isinstance(pending, list):
                raise ValueError
            fields["pending_revisions"] = (
                tuple(pending) if pending is not None else None
            )
            classifications = fields["classifications"]
            if not isinstance(classifications, list) or not all(
                isinstance(item, dict) for item in classifications
            ):
                raise ValueError
            fields["classifications"] = tuple(classifications)
            return cls(**fields)
        except (KeyError, TypeError, ValueError):
            raise PreparationError("invalid_prepared_migration") from None


@dataclass(frozen=True)
class SchemaMetadata:
    head: str
    tree_oid: str
    revision_map: RevisionMap
    paths: Mapping[str, str]
    sources: Mapping[str, bytes]

    def pending(self, start: str) -> tuple[str, ...]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                return tuple(
                    item.revision
                    for item in reversed(
                        tuple(self.revision_map.iterate_revisions(self.head, start))
                    )
                )
        except Exception:
            raise PreparationError("unresolvable_revision_range") from None


class GitReleaseRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        git = shutil.which("git")
        if git is None:
            raise PreparationError("git_unavailable")
        self._git_path = git

    def _git(self, *args: str, data: bytes | None = None) -> bytes:
        try:
            completed = subprocess.run(  # noqa: S603
                [self._git_path, "-C", str(self.root), *args],
                input=data,
                capture_output=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            raise PreparationError("git_read_failed") from None
        if completed.returncode:
            raise PreparationError("git_read_failed")
        return completed.stdout

    def ensure_main(self, sha: str) -> None:
        require_sha(sha)
        self._git("merge-base", "--is-ancestor", sha, "refs/remotes/origin/main")

    def parent(self, sha: str) -> str:
        require_sha(sha)
        return require_sha(self._git("rev-parse", f"{sha}^1").decode().strip())

    def changed_paths(self, parent: str, sha: str) -> tuple[str, ...]:
        require_sha(parent)
        require_sha(sha)
        return tuple(
            item.decode("utf-8")
            for item in self._git(
                "diff", "--no-renames", "--name-only", "-z", parent, sha, "--"
            ).split(b"\0")
            if item
        )

    def migration_files(self, sha: str) -> dict[str, tuple[str, bytes]]:
        require_sha(sha)
        rows = self._git("ls-tree", "-rz", f"{sha}:{_VERSIONS}").split(b"\0")
        entries: list[tuple[str, str, str]] = []
        for row in filter(None, rows):
            metadata, raw_path = row.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
            parsed = PurePosixPath(path)
            if (
                kind != "blob"
                or mode not in {"100644", "100755", "120000"}
                or parsed.is_absolute()
                or ".." in parsed.parts
            ):
                raise PreparationError("unsupported_migration_tree_entry")
            entries.append((path, mode, oid))
        raw = self._git(
            "cat-file",
            "--batch",
            data="".join(f"{oid}\n" for _, _, oid in entries).encode(),
        )
        files: dict[str, tuple[str, bytes]] = {}
        position = 0
        for path, mode, oid in entries:
            end = raw.index(b"\n", position)
            header = raw[position:end].decode("ascii").split()
            if len(header) != 3 or header[:2] != [oid, "blob"]:
                raise PreparationError("git_blob_mismatch")
            size = int(header[2])
            position = end + 1
            files[path] = (mode, raw[position : position + size])
            position += size + 1
        return files

    def schema(self, sha: str) -> SchemaMetadata:
        files = self.migration_files(sha)
        tree = require_sha(
            self._git("rev-parse", f"{sha}:{_VERSIONS}").decode().strip()
        )
        revisions: list[Revision] = []
        paths: dict[str, str] = {}
        sources: dict[str, bytes] = {}
        try:
            for path, (mode, source) in files.items():
                if "/" in path or not path.endswith(".py") or path == "__init__.py":
                    continue
                if mode == "120000":
                    raise ValueError
                metadata = _revision_metadata(source)
                revision = require_revision(metadata["revision"])
                if revision in paths:
                    raise ValueError
                revisions.append(
                    Revision(
                        revision,
                        metadata["down_revision"],
                        dependencies=metadata.get("depends_on"),
                        branch_labels=metadata.get("branch_labels"),
                    )
                )
                paths[revision] = path
                sources[revision] = source
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                graph = RevisionMap(lambda: iter(revisions))
                heads = graph.heads
            if len(heads) != 1:
                raise ValueError
        except Exception:
            raise PreparationError("invalid_migration_graph") from None
        return SchemaMetadata(heads[0], tree, graph, paths, sources)


def _revision_metadata(source: bytes) -> dict[str, object]:
    fields: dict[str, object] = {}
    names = {"revision", "down_revision", "depends_on", "branch_labels"}
    for statement in ast.parse(source).body:
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
            if isinstance(statement, ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id in names:
                if target.id in fields:
                    raise ValueError
                value = ast.literal_eval(statement.value)
                if value is not None:
                    for revision in (
                        value if isinstance(value, (tuple, list)) else (value,)
                    ):
                        require_revision(revision)
                fields[target.id] = tuple(value) if isinstance(value, list) else value
    if not isinstance(fields.get("revision"), str) or "down_revision" not in fields:
        raise ValueError
    return fields


def require_successful_workflows(github: GitHubApi, sha: str) -> None:
    require_sha(sha)
    try:
        for workflow in ("ci.yml", "security-pr.yml"):
            runs: list[Mapping[str, object]] = []
            page = 1
            while True:
                query = urlencode(
                    {
                        "head_sha": sha,
                        "branch": "main",
                        "event": "push",
                        "per_page": 100,
                        "page": page,
                    }
                )
                response = github.get_json(
                    f"/actions/workflows/{workflow}/runs?{query}"
                )
                if not isinstance(response, Mapping) or not isinstance(
                    response.get("workflow_runs"), list
                ):
                    raise ValueError
                batch = response["workflow_runs"]
                for run in batch:
                    if not isinstance(run, Mapping):
                        raise ValueError
                    if (
                        run.get("head_sha") == sha
                        and run.get("head_branch") == "main"
                        and run.get("event") == "push"
                    ):
                        if run.get("path") != f".github/workflows/{workflow}":
                            raise ValueError
                        runs.append(run)
                if len(batch) < 100:
                    break
                page += 1
                if page > 10:
                    raise ValueError
            if not runs:
                raise ValueError

            def order(run: Mapping[str, object]) -> tuple[datetime, int, int]:
                if not _positive(run.get("id")) or not _positive(
                    run.get("run_attempt")
                ):
                    raise ValueError
                timestamp = datetime.fromisoformat(
                    str(run["created_at"]).replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None:
                    raise ValueError
                return timestamp, run["id"], run["run_attempt"]

            latest = max(runs, key=order)
            if (
                latest.get("status") != "completed"
                or latest.get("conclusion") != "success"
            ):
                raise ValueError
    except Exception:
        raise PreparationError("required_workflow_not_successful") from None


def _classifications(
    schema: SchemaMetadata, pending: tuple[str, ...]
) -> tuple[str, tuple[dict[str, object], ...]]:
    with tempfile.TemporaryDirectory(prefix="vector-migration-classify-") as directory:
        paths = []
        for revision in pending:
            path = Path(directory) / schema.paths[revision]
            path.write_bytes(schema.sources[revision])
            paths.append(path)
        decision = decide_changed_migrations(paths)
        results = tuple(
            {
                "revision": revision,
                "path": f"{_VERSIONS}/{schema.paths[revision]}",
                "kind": item.kind,
                "declared_kind": item.declared_kind,
                "reasons": list(item.reasons),
            }
            for revision, item in zip(pending, decision.revisions, strict=True)
        )
    return decision.decision, results


def _prepare_from_snapshot(
    repository: GitReleaseRepository,
    snapshot: LedgerSnapshot,
    *,
    release_sha: str,
    mode: str,
    run_id: int,
    run_attempt: int,
) -> PreparedMigration:
    require_sha(release_sha)
    if mode not in _MODES or not _positive(run_id) or not _positive(run_attempt):
        raise PreparationError("invalid_migration_request")
    repository.ensure_main(release_sha)
    if snapshot.baseline is None:
        raise PreparationError("baseline_unobservable")
    record = snapshot.record if snapshot.state == "available" else None
    if mode != "verify" and record is None:
        raise PreparationError("usable_start_record_required")
    schema = repository.schema(release_sha)
    start = record.target_revision if record else None
    pending = schema.pending(start) if start else None
    decision, classifications = _classifications(schema, pending or ())
    if decision == "invalid":
        raise PreparationError("invalid_pending_range")
    if mode == "verify" and pending:
        raise PreparationError("verify_not_at_target")
    if pending and decision != {"expand": "expand", "contract": "manual"}.get(mode):
        raise PreparationError("mode_range_mismatch")
    parent = repository.parent(release_sha) if mode == "contract" else None
    if mode == "contract" and pending:
        for path in repository.changed_paths(parent, release_sha):
            if not is_contract_path_allowed(path):
                raise PreparationError("contract_contains_runtime_changes")
        parent_schema = repository.schema(parent)
        if not snapshot.allows_rollout(parent_schema.head, parent_schema.tree_oid):
            raise PreparationError("contract_parent_schema_mismatch")
    return PreparedMigration(
        release_sha,
        mode,
        schema.head,
        schema.tree_oid,
        start,
        snapshot,
        parent,
        run_id,
        run_attempt,
        "no_changes" if mode != "verify" and not pending else "ready",
        pending,
        classifications,
    )


def prepare_migration(
    repository: GitReleaseRepository,
    github: GitHubApi,
    ledger: MigrationLedger,
    *,
    release_sha: str,
    mode: str,
    run_id: int,
    run_attempt: int,
) -> PreparedMigration:
    require_sha(release_sha)
    repository.ensure_main(release_sha)
    require_successful_workflows(github, release_sha)
    return _prepare_from_snapshot(
        repository,
        ledger.read_latest(),
        release_sha=release_sha,
        mode=mode,
        run_id=run_id,
        run_attempt=run_attempt,
    )


def validate_preparation(
    prepared: PreparedMigration,
    repository: GitReleaseRepository,
    *,
    release_sha: str,
    mode: str,
    run_id: int,
    run_attempt: int,
) -> None:
    if (prepared.release_sha, prepared.mode, prepared.run_id, prepared.run_attempt) != (
        release_sha,
        mode,
        run_id,
        run_attempt,
    ):
        raise PreparationError("prepared_identity_mismatch")
    current = _prepare_from_snapshot(
        repository,
        prepared.baseline,
        release_sha=release_sha,
        mode=mode,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    if current != prepared:
        raise PreparationError("prepared_metadata_mismatch")


def revalidate_preparation(
    prepared: PreparedMigration, repository: GitReleaseRepository, github: GitHubApi
) -> None:
    validate_preparation(
        prepared,
        repository,
        release_sha=prepared.release_sha,
        mode=prepared.mode,
        run_id=prepared.run_id,
        run_attempt=prepared.run_attempt,
    )
    require_successful_workflows(github, prepared.release_sha)


def render_summary(prepared: PreparedMigration) -> str:
    def safe(value: object) -> str:
        return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ")

    baseline = prepared.baseline.baseline
    rows = {
        "release SHA": prepared.release_sha,
        "mode": prepared.mode,
        "target head": prepared.target_revision,
        "migration tree": prepared.migration_tree_oid,
        "期待開始revision（ledger）": prepared.expected_start_revision or "未確認",
        "baseline Deployment / status": (
            f"{baseline.deployment_id} / {baseline.status_id}"
        ),
        "予定range": ", ".join(prepared.pending_revisions)
        if prepared.pending_revisions is not None
        else "未確認",
        "contract直前SHA": prepared.contract_parent_sha or "対象外",
        "準備結果": prepared.result,
    }
    lines = [
        "## Migration prepare",
        "",
        "本番DBには接続していません。承認前にこのsummaryを確認してください。",
        "",
        "CI / Securityはmain workflowの成功を確認済みです"
        "（PR専用テストのskipを含みます）。",
        "",
        "| 項目 | 期待値 |",
        "| --- | --- |",
    ]
    lines.extend(f"| {safe(key)} | {safe(value)} |" for key, value in rows.items())
    lines.extend(["", "### 分類", ""])
    for item in prepared.classifications:
        lines.append(
            f"- {safe(item['revision'])}: {safe(item['kind'])}; "
            f"{safe(', '.join(item['reasons']) or '明示分類・安全条件を確認')}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    validate = bool(arguments and arguments[0] == "validate")
    if validate:
        arguments.pop(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--mode", choices=sorted(_MODES), required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    if validate:
        parser.add_argument("--prepared", type=Path, required=True)
    else:
        parser.add_argument("--repo", required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--summary-file", type=Path, required=True)
        parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        repository = GitReleaseRepository(args.repo_root)
        request = dict(
            release_sha=args.release_sha,
            mode=args.mode,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
        if validate:
            prepared = PreparedMigration.from_dict(
                json.loads(args.prepared.read_text())
            )
            validate_preparation(prepared, repository, **request)
        else:
            github = GitHubCli(args.repo)
            prepared = prepare_migration(
                repository, github, MigrationLedger(github), **request
            )
            args.output.write_text(json.dumps(prepared.as_dict()) + "\n")
            with args.summary_file.open("a") as summary:
                summary.write(render_summary(prepared))
            with args.github_output.open("a") as output:
                output.write(f"result={prepared.result}\n")
        return 0
    except Exception:
        print("::error::migration_preparation_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
