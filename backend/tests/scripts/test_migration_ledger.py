"""DB適用記録の4つの不変条件を固定する。"""

from __future__ import annotations

import copy
import importlib.util
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "migration_ledger", _ROOT / ".github/scripts/migration_ledger.py"
)
assert _SPEC is not None and _SPEC.loader is not None
ledger = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ledger
_SPEC.loader.exec_module(ledger)

pytestmark = pytest.mark.unit
_SHA = "a" * 40
_TREE = "b" * 40


def _payload(**overrides: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "release_sha": _SHA,
        "mode": "verify",
        "expected_start_revision": None,
        "target_revision": "r1",
        "migration_tree_oid": _TREE,
        "github_run_id": 123,
        "github_run_attempt": 1,
        "baseline_deployment_id": None,
        "baseline_status_id": None,
        **overrides,
    }


def _timestamp(number: int) -> str:
    return (datetime(2026, 9, 2, tzinfo=UTC) + timedelta(seconds=number)).isoformat()


def _deployment(number: int, **overrides: object) -> dict[str, object]:
    return {
        "id": number,
        "created_at": _timestamp(number),
        "environment": "db-migration",
        "task": "deploy:migrations",
        "sha": _SHA,
        "payload": _payload(),
        **overrides,
    }


def _status(number: int, state: str) -> dict[str, object]:
    return {"id": number, "created_at": _timestamp(number), "state": state}


class FakeGitHub:
    def __init__(self, deployments=(), statuses=None, *, fail_at=None):
        self.deployments = list(copy.deepcopy(deployments))
        self.statuses = copy.deepcopy(statuses or {})
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.fail_at = fail_at
        self.created_id: int | None = None

    def get_json(self, path):
        url = urlsplit(path)
        parts = url.path.strip("/").split("/")
        if len(parts) == 2:
            number = int(parts[1])
            if self.fail_at == "get_created" and number == self.created_id:
                raise RuntimeError("private response must not escape")
            return copy.deepcopy(next(d for d in self.deployments if d["id"] == number))
        values = (
            self.deployments
            if len(parts) == 1
            else self.statuses.get(int(parts[1]), [])
        )
        query = parse_qs(url.query)
        page = int(query.get("page", ["1"])[0])
        size = int(query.get("per_page", ["100"])[0])
        return copy.deepcopy(values[(page - 1) * size : page * size])

    def post_json(self, path, body):
        self.posts.append((path, copy.deepcopy(body)))
        if path == "/deployments":
            if self.fail_at == "create":
                raise RuntimeError("private response must not escape")
            number = max([99, *(d["id"] for d in self.deployments)]) + 1
            self.created_id = number
            result = _deployment(number, **body, sha=body["ref"])
            self.deployments.append(result)
            return copy.deepcopy(result)
        if self.fail_at == "progress" and body["state"] == "in_progress":
            raise RuntimeError("private response must not escape")
        number = int(path.split("/")[2])
        statuses = self.statuses.setdefault(number, [])
        result = _status(1000 + len(self.posts), body["state"])
        statuses.append(result)
        return copy.deepcopy(result)


def _record(prepared, **overrides):
    baseline = prepared.baseline
    return ledger.LedgerRecord(
        **_payload(
            baseline_deployment_id=baseline.deployment_id if baseline else None,
            baseline_status_id=baseline.status_id if baseline else None,
            **overrides,
        )
    )


def test_latest_versioned_success_is_available() -> None:
    api = FakeGitHub([_deployment(1)], {1: [_status(10, "success")]})

    result = ledger.MigrationLedger(api).read_latest()

    assert (result.state, result.baseline, result.record) == (
        "available",
        ledger.Baseline(1, 10),
        ledger.LedgerRecord(**_payload()),
    )


def test_latest_failure_never_falls_back_to_older_success() -> None:
    api = FakeGitHub(
        [_deployment(1), _deployment(2)],
        {1: [_status(10, "success")], 2: [_status(20, "failure")]},
    )

    result = ledger.MigrationLedger(api).read_latest()

    assert (result.state, result.baseline) == ("unavailable", ledger.Baseline(2, 20))


def test_latest_without_status_never_falls_back_to_older_success() -> None:
    api = FakeGitHub([_deployment(1), _deployment(2)], {1: [_status(10, "success")]})

    result = ledger.MigrationLedger(api).read_latest()

    assert (result.state, result.baseline) == ("unavailable", ledger.Baseline(2, None))


@pytest.mark.parametrize(
    "other",
    [{"environment": "production-migration"}, {"task": "deploy"}],
)
def test_other_environment_or_task_is_not_ledger(other) -> None:
    api = FakeGitHub([_deployment(1, **other)], {1: [_status(10, "success")]})

    result = ledger.MigrationLedger(api).read_latest()

    assert (result.state, result.baseline) == ("missing", ledger.Baseline(None, None))


@pytest.mark.parametrize("payload", [{}, _payload(schema_version=2)])
def test_legacy_or_unreadable_latest_record_is_unavailable(payload) -> None:
    api = FakeGitHub(
        [_deployment(1), _deployment(2, payload=payload)],
        {1: [_status(10, "success")], 2: [_status(20, "success")]},
    )

    result = ledger.MigrationLedger(api).read_latest()

    assert (result.state, result.baseline, result.record) == (
        "unavailable",
        ledger.Baseline(2, 20),
        None,
    )


def test_api_failure_is_not_missing_and_cannot_start_verify(
    monkeypatch, capsys
) -> None:
    def failed_request(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "private body", "private token")

    monkeypatch.setattr(ledger.shutil, "which", lambda _: "/test/gh")
    monkeypatch.setattr(ledger.subprocess, "run", failed_request)
    subject = ledger.MigrationLedger(ledger.GitHubCli("owner/repo"))
    prepared = subject.read_latest()
    with pytest.raises(ledger.LedgerError) as raised:
        subject.begin(prepared, _record(prepared))
    output = capsys.readouterr()

    assert (
        prepared.state,
        prepared.baseline,
        "private" in repr(prepared) + str(raised.value) + output.out + output.err,
    ) == ("unavailable", None, False)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    return subprocess.check_output(  # noqa: S603
        [git, *args], cwd=repo, text=True, stderr=subprocess.DEVNULL
    ).strip()


@pytest.mark.parametrize(
    ("revision", "different_tree", "allowed"),
    [("r1", False, True), ("r2", False, False), ("r1", True, False)],
)
def test_rollout_matches_revision_and_tree_not_release_sha(
    tmp_path, revision, different_tree, allowed
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Ledger test")
    versions = tmp_path / "backend/alembic/versions"
    versions.mkdir(parents=True)
    (versions / "r1.py").write_text("revision = 'r1'\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "schema")
    first = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "application.py").write_text("pass\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "code")
    second = _git(tmp_path, "rev-parse", "HEAD")
    tree = ledger.migration_tree_oid(tmp_path, first)
    target_tree = ledger.migration_tree_oid(tmp_path, second)
    api = FakeGitHub(
        [
            _deployment(
                1,
                sha=first,
                payload=_payload(release_sha=first, migration_tree_oid=tree),
            )
        ],
        {1: [_status(10, "success")]},
    )
    result = ledger.MigrationLedger(api).read_latest()

    assert (
        first != second,
        tree == target_tree,
        result.allows_rollout(revision, "c" * 40 if different_tree else target_tree),
    ) == (True, True, allowed)


@pytest.mark.parametrize("mode", ["verify", "expand", "contract"])
def test_only_verify_can_start_without_a_record(mode) -> None:
    api = FakeGitHub()
    subject = ledger.MigrationLedger(api)
    prepared = subject.read_latest()
    record = _record(
        prepared, mode=mode, expected_start_revision=None if mode == "verify" else "r0"
    )
    if mode == "verify":
        started = subject.begin(prepared, record)
        assert (started.deployment_id, subject.read_latest().state) == (
            api.created_id,
            "unavailable",
        )
    else:
        with pytest.raises(ledger.LedgerError):
            subject.begin(prepared, record)
        assert api.posts == []


@pytest.mark.parametrize("change", ["deployment", "status"])
def test_changed_baseline_prevents_start(change) -> None:
    api = FakeGitHub([_deployment(1)], {1: [_status(10, "success")]})
    subject = ledger.MigrationLedger(api)
    prepared = subject.read_latest()
    if change == "deployment":
        api.deployments.append(_deployment(2))
    else:
        api.statuses[1].append(_status(11, "failure"))
    with pytest.raises(ledger.LedgerError):
        subject.begin(prepared, _record(prepared))

    assert api.posts == []


@pytest.mark.parametrize("fail_at", [None, "create", "get_created", "progress"])
def test_start_succeeds_only_after_the_whole_write_sequence(fail_at) -> None:
    api = FakeGitHub(fail_at=fail_at)
    subject = ledger.MigrationLedger(api)
    prepared = subject.read_latest()
    started = None
    if fail_at:
        with pytest.raises(ledger.LedgerError):
            subject.begin(prepared, _record(prepared))
        api.fail_at = None
    else:
        started = subject.begin(prepared, _record(prepared))
        subject.finish(started, "success")
    expected_create = {
        "ref": _SHA,
        "environment": "db-migration",
        "task": "deploy:migrations",
        "auto_merge": False,
        "required_contexts": [],
        "production_environment": False,
        "payload": _payload(),
    }

    assert (
        started is not None,
        subject.read_latest().state,
        api.posts[0],
        all(
            path == f"/deployments/{api.created_id}/statuses"
            for path, _ in api.posts[1:]
        ),
    ) == (
        fail_at is None,
        "available"
        if fail_at is None
        else "missing"
        if fail_at == "create"
        else "unavailable",
        ("/deployments", expected_create),
        True,
    )
