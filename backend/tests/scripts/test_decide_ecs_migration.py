"""本番migration one-off taskの起動判定。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "decide_ecs_migration.py"
_SHA = "a" * 40
_OTHER = "b" * 40

pytestmark = pytest.mark.unit


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("decide_ecs_migration", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


decide = _load_module()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo_with_versions(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "ci")
    _git(repo, "config", "commit.gpgsign", "false")
    versions = repo / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "a1.py").write_text("revision = 'a1'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_should_run_when_last_migrated_sha_is_missing(tmp_path: Path) -> None:
    repo, release = _repo_with_versions(tmp_path)

    assert decide.should_run_migration(
        repo=repo,
        release_sha=release,
        last_migrated_sha=None,
        force=False,
    )


def test_should_skip_when_alembic_versions_are_unchanged(tmp_path: Path) -> None:
    repo, last = _repo_with_versions(tmp_path)
    (repo / "app.py").write_text("print(1)\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "app")
    release = _git(repo, "rev-parse", "HEAD")

    assert not decide.should_run_migration(
        repo=repo,
        release_sha=release,
        last_migrated_sha=last,
        force=False,
    )


def test_should_run_when_alembic_versions_differ(tmp_path: Path) -> None:
    repo, last = _repo_with_versions(tmp_path)
    versions = repo / "backend" / "alembic" / "versions"
    (versions / "a2.py").write_text("revision = 'a2'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "migration")
    release = _git(repo, "rev-parse", "HEAD")

    assert decide.should_run_migration(
        repo=repo,
        release_sha=release,
        last_migrated_sha=last,
        force=False,
    )


def test_force_runs_even_when_versions_are_unchanged(tmp_path: Path) -> None:
    repo, last = _repo_with_versions(tmp_path)

    assert decide.should_run_migration(
        repo=repo,
        release_sha=last,
        last_migrated_sha=last,
        force=True,
    )


def test_force_decide_skips_github_ledger_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, release = _repo_with_versions(tmp_path)
    output = tmp_path / "github-output"

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ledger must not be fetched when force is true")

    monkeypatch.setattr(decide, "fetch_last_successful_sha", boom)
    monkeypatch.setattr(decide, "_github_api", boom)

    assert (
        decide.main(
            [
                "decide",
                "--release-sha",
                release,
                "--force",
                "true",
                "--github-repo",
                "yook11/Vector",
                "--repo-dir",
                str(repo),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_text(encoding="utf-8") == "run=true\n"


def test_missing_last_commit_object_is_fail_closed_run(tmp_path: Path) -> None:
    repo, release = _repo_with_versions(tmp_path)

    assert decide.should_run_migration(
        repo=repo,
        release_sha=release,
        last_migrated_sha=_OTHER,
        force=False,
    )


def test_latest_successful_sha_skips_orphan_without_success_status() -> None:
    orphan_id = 11
    success_id = 10
    deployments: Sequence[Mapping[str, object]] = (
        {
            "id": orphan_id,
            "sha": _SHA,
            "environment": decide.LEDGER_ENVIRONMENT,
            "task": decide.LEDGER_TASK,
        },
        {
            "id": success_id,
            "sha": _OTHER,
            "environment": decide.LEDGER_ENVIRONMENT,
            "task": decide.LEDGER_TASK,
        },
    )
    statuses = {
        orphan_id: (),
        success_id: ({"state": "success"},),
    }

    assert decide.latest_successful_sha(deployments, statuses) == _OTHER


def test_latest_successful_sha_ignores_other_environment_and_task() -> None:
    deployments: Sequence[Mapping[str, object]] = (
        {
            "id": 1,
            "sha": _SHA,
            "environment": "production",
            "task": decide.LEDGER_TASK,
        },
        {
            "id": 2,
            "sha": _OTHER,
            "environment": decide.LEDGER_ENVIRONMENT,
            "task": "deploy",
        },
    )
    statuses = {
        1: ({"state": "success"},),
        2: ({"state": "success"},),
    }

    assert decide.latest_successful_sha(deployments, statuses) is None


def test_ledger_create_body_overrides_deployment_defaults() -> None:
    assert decide.ledger_create_body(_SHA) == {
        "ref": _SHA,
        "environment": "db-migration",
        "task": "deploy:migrations",
        "auto_merge": False,
        "required_contexts": [],
        "production_environment": False,
    }


class _FakeGitHubApi:
    def __init__(self) -> None:
        self.posts: list[tuple[str, Mapping[str, object]]] = []

    def get_json(self, path: str) -> object:
        raise AssertionError(path)

    def post_json(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        self.posts.append((path, body))
        if path == "/deployments":
            return {"id": 99}
        return {"id": 1}


def test_record_successful_migration_writes_success_status() -> None:
    client = _FakeGitHubApi()

    decide.record_successful_migration(client, _SHA)

    assert client.posts == [
        ("/deployments", decide.ledger_create_body(_SHA)),
        ("/deployments/99/statuses", {"state": "success"}),
    ]


def test_record_successful_migration_stops_when_create_omits_id() -> None:
    class _BrokenCreate(_FakeGitHubApi):
        def post_json(
            self, path: str, body: Mapping[str, object]
        ) -> Mapping[str, object]:
            self.posts.append((path, body))
            return {}

    client = _BrokenCreate()

    with pytest.raises(decide.DecideInputError, match="id"):
        decide.record_successful_migration(client, _SHA)
    assert [path for path, _body in client.posts] == ["/deployments"]
