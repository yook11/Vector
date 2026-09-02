"""prepareは実DBを観測せず、承認対象の期待値だけを記録する。"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / ".github" / "scripts"))
sys.path.insert(0, str(_ROOT / "backend"))

prepare = importlib.import_module("migration_prepare")


pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    return subprocess.check_output(  # noqa: S603
        [git, *args], cwd=repo, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _revision(revision: str, parent: str | None, kind: str | None) -> str:
    declaration = f'MIGRATION_KIND = "{kind}"\n' if kind is not None else ""
    operation = "op.drop_table('obsolete')" if kind == "contract" else "pass"
    return (
        f'revision = "{revision}"\n'
        f"down_revision = {parent!r}\n"
        f"{declaration}\n"
        "def upgrade() -> None:\n"
        f"    {operation}\n"
    )


class GitFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.email", "test@example.invalid")
        _git(root, "config", "user.name", "Migration prepare test")
        self.versions = root / "backend" / "alembic" / "versions"
        self.versions.mkdir(parents=True)
        self.write_revision("r1", None, "expand")
        self.r1 = self.commit("initial schema")
        self._mark_main()

    def write_revision(
        self, revision: str, parent: str | None, kind: str | None
    ) -> None:
        (self.versions / f"{revision}.py").write_text(
            _revision(revision, parent, kind), encoding="utf-8"
        )

    def commit(self, message: str) -> str:
        _git(self.root, "add", ".")
        _git(self.root, "-c", "commit.gpgsign=false", "commit", "-m", message)
        sha = _git(self.root, "rev-parse", "HEAD")
        self._mark_main()
        return sha

    def _mark_main(self) -> None:
        _git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")


def _repository(fixture: GitFixture) -> prepare.GitReleaseRepository:
    return prepare.GitReleaseRepository(fixture.root)


def _timestamp(number: int) -> str:
    return (datetime(2026, 9, 2, tzinfo=UTC) + timedelta(seconds=number)).isoformat()


def _run(
    sha: str, workflow: str, number: int, *, conclusion: str = "success"
) -> dict[str, object]:
    return {
        "id": number,
        "run_attempt": 1,
        "created_at": _timestamp(number),
        "head_sha": sha,
        "head_branch": "main",
        "event": "push",
        "path": f".github/workflows/{workflow}",
        "status": "completed",
        "conclusion": conclusion,
    }


class FakeGitHub:
    def __init__(self, sha: str) -> None:
        self.sha = sha
        self.runs = {
            "ci.yml": [_run(sha, "ci.yml", 10)],
            "security-pr.yml": [_run(sha, "security-pr.yml", 20)],
        }

    def get_json(self, path: str) -> object:
        workflow = urlsplit(path).path.split("/")[3]
        return {"workflow_runs": list(self.runs[workflow])}


class FakeLedger:
    def __init__(self, snapshot: prepare.LedgerSnapshot) -> None:
        self.snapshot = snapshot
        self.reads = 0

    def read_latest(self) -> prepare.LedgerSnapshot:
        self.reads += 1
        return self.snapshot


def _available_snapshot(
    repository: prepare.GitReleaseRepository, sha: str, revision: str
) -> prepare.LedgerSnapshot:
    schema = repository.schema(sha)
    record = prepare.LedgerRecord(
        schema_version=1,
        release_sha=sha,
        mode="verify",
        expected_start_revision=None,
        target_revision=revision,
        migration_tree_oid=schema.tree_oid,
        github_run_id=99,
        github_run_attempt=1,
        baseline_deployment_id=None,
        baseline_status_id=None,
    )
    return prepare.LedgerSnapshot(
        "available", prepare.Baseline(700, 701), record, "success"
    )


def _prepare(
    repository: prepare.GitReleaseRepository,
    github: FakeGitHub,
    snapshot: prepare.LedgerSnapshot,
    *,
    sha: str,
    mode: str,
) -> prepare.PreparedMigration:
    return prepare.prepare_migration(
        repository,
        github,
        FakeLedger(snapshot),
        release_sha=sha,
        mode=mode,
        run_id=123,
        run_attempt=2,
    )


def test_prepare_expand_uses_the_ledger_revision_as_an_expected_start(
    tmp_path: Path,
) -> None:
    fixture = GitFixture(tmp_path)
    fixture.write_revision("r2", "r1", "expand")
    r2 = fixture.commit("expand schema")
    repository = _repository(fixture)
    snapshot = _available_snapshot(repository, fixture.r1, "r1")

    prepared = _prepare(repository, FakeGitHub(r2), snapshot, sha=r2, mode="expand")

    assert (
        prepared.result,
        prepared.target_revision,
        prepared.expected_start_revision,
        prepared.pending_revisions,
        [item["kind"] for item in prepared.classifications],
    ) == ("ready", "r2", "r1", ("r2",), ["expand"])


def test_prepare_verify_recovery_keeps_live_range_unknown_in_its_summary(
    tmp_path: Path,
) -> None:
    fixture = GitFixture(tmp_path)
    repository = _repository(fixture)
    snapshot = prepare.LedgerSnapshot("missing", prepare.Baseline(None, None))

    prepared = _prepare(
        repository, FakeGitHub(fixture.r1), snapshot, sha=fixture.r1, mode="verify"
    )

    assert (prepared.expected_start_revision, prepared.pending_revisions) == (
        None,
        None,
    )
    assert "本番DBには接続していません" in prepare.render_summary(prepared)
    assert "| 予定range | 未確認 |" in prepare.render_summary(prepared)


@pytest.mark.parametrize(
    ("mode", "release_kind", "expectation"),
    [
        ("contract", "expand", "mode_range_mismatch"),
        ("expand", None, "invalid_pending_range"),
    ],
)
def test_prepare_rejects_mismatched_or_unclassified_ranges(
    tmp_path: Path, mode: str, release_kind: str | None, expectation: str
) -> None:
    fixture = GitFixture(tmp_path)
    fixture.write_revision("r2", "r1", release_kind)
    r2 = fixture.commit("unsafe schema")
    repository = _repository(fixture)

    with pytest.raises(prepare.PreparationError, match=expectation):
        _prepare(
            repository,
            FakeGitHub(r2),
            _available_snapshot(repository, fixture.r1, "r1"),
            sha=r2,
            mode=mode,
        )


def test_prepare_rejects_a_mixed_pending_range_before_approval(tmp_path: Path) -> None:
    fixture = GitFixture(tmp_path)
    fixture.write_revision("r2", "r1", "expand")
    fixture.commit("expand schema")
    fixture.write_revision("r3", "r2", "contract")
    r3 = fixture.commit("contract schema")
    repository = _repository(fixture)

    with pytest.raises(prepare.PreparationError, match="invalid_pending_range"):
        _prepare(
            repository,
            FakeGitHub(r3),
            _available_snapshot(repository, fixture.r1, "r1"),
            sha=r3,
            mode="contract",
        )


@pytest.mark.parametrize("mode", ["expand", "contract"])
def test_empty_apply_range_is_no_changes_and_does_not_need_a_write(
    tmp_path: Path, mode: str
) -> None:
    fixture = GitFixture(tmp_path)
    release_sha = fixture.r1
    if mode == "contract":
        (fixture.root / "README.md").write_text("release metadata\n", encoding="utf-8")
        release_sha = fixture.commit("contract release without schema change")
    repository = _repository(fixture)
    snapshot = _available_snapshot(repository, fixture.r1, "r1")
    ledger_client = FakeLedger(snapshot)

    prepared = prepare.prepare_migration(
        repository,
        FakeGitHub(release_sha),
        ledger_client,
        release_sha=release_sha,
        mode=mode,
        run_id=123,
        run_attempt=2,
    )

    assert (prepared.result, prepared.pending_revisions, ledger_client.reads) == (
        "no_changes",
        (),
        1,
    )


@pytest.mark.parametrize(
    ("mode", "snapshot", "expectation"),
    [
        (
            "expand",
            prepare.LedgerSnapshot("missing", prepare.Baseline(None, None)),
            "usable_start_record_required",
        ),
        (
            "verify",
            prepare.LedgerSnapshot("unavailable", None, reason="github_read_failed"),
            "baseline_unobservable",
        ),
    ],
)
def test_missing_or_unobservable_ledger_does_not_start_an_ordinary_apply(
    tmp_path: Path,
    mode: str,
    snapshot: prepare.LedgerSnapshot,
    expectation: str,
) -> None:
    fixture = GitFixture(tmp_path)
    repository = _repository(fixture)

    with pytest.raises(prepare.PreparationError, match=expectation):
        _prepare(
            repository,
            FakeGitHub(fixture.r1),
            snapshot,
            sha=fixture.r1,
            mode=mode,
        )


def test_contract_requires_parent_schema_and_no_runtime_changes(tmp_path: Path) -> None:
    fixture = GitFixture(tmp_path)
    fixture.write_revision("r2", "r1", "expand")
    r2 = fixture.commit("expand schema")
    fixture.write_revision("r3", "r2", "contract")
    for name in (
        "backend/tests/fixtures/helper.py",
        "frontend/src/feature.test.tsx",
        "frontend/src/test/mock.ts",
        "frontend/e2e/fixtures/user.ts",
        "frontend/vitest.setup.node.ts",
    ):
        path = fixture.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test support\n", encoding="utf-8")
    r3 = fixture.commit("contract schema")
    repository = _repository(fixture)

    prepared = _prepare(
        repository,
        FakeGitHub(r3),
        _available_snapshot(repository, r2, "r2"),
        sha=r3,
        mode="contract",
    )

    assert (prepared.contract_parent_sha, prepared.pending_revisions) == (r2, ("r3",))

    (fixture.root / "backend" / "app.py").write_text("changed\n", encoding="utf-8")
    unsafe_contract = fixture.commit("runtime change in contract")
    with pytest.raises(
        prepare.PreparationError, match="contract_contains_runtime_changes"
    ):
        _prepare(
            repository,
            FakeGitHub(unsafe_contract),
            _available_snapshot(repository, r2, "r2"),
            sha=unsafe_contract,
            mode="contract",
        )


def test_prepared_report_is_strictly_decoded_and_revalidation_rejects_newer_failure(
    tmp_path: Path,
) -> None:
    fixture = GitFixture(tmp_path)
    repository = _repository(fixture)
    github = FakeGitHub(fixture.r1)
    prepared = _prepare(
        repository,
        github,
        _available_snapshot(repository, fixture.r1, "r1"),
        sha=fixture.r1,
        mode="verify",
    )
    report = prepared.as_dict()
    report["baseline"] = {"baseline": {"deployment_id": "700", "status_id": 701}}

    with pytest.raises(prepare.PreparationError, match="invalid_prepared_migration"):
        prepare.PreparedMigration.from_dict(report)

    github.runs["ci.yml"].append(_run(fixture.r1, "ci.yml", 30, conclusion="failure"))
    with pytest.raises(
        prepare.PreparationError, match="required_workflow_not_successful"
    ):
        prepare.revalidate_preparation(prepared, repository, github)
