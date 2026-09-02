"""最新mainと適用済みschemaの確認を、実際のservice更新境界で検証する。"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / ".github/scripts"))
guard = importlib.import_module("check_app_release")
sys.path.remove(str(_ROOT / ".github/scripts"))

pytestmark = pytest.mark.unit
_SHA = "a" * 40
_TREE = "b" * 40


@dataclass
class FakeGitHub:
    release_sha: str = _SHA
    tree_oid: str = _TREE
    main_sha: str = _SHA
    conclusion: str = "success"
    ledger_state: str = "success"
    ledger_revision: str = "r1"
    ledger_tree: str = _TREE
    change_during_registration: str = "none"

    def get_json(self, path: str) -> object:
        path = urlsplit(path).path
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.main_sha}}
        if path.startswith("/actions/workflows/"):
            workflow = path.split("/")[3]
            return {
                "workflow_runs": [
                    {
                        "id": 1,
                        "run_attempt": 1,
                        "created_at": "2026-09-01T00:00:00Z",
                        "head_sha": self.release_sha,
                        "head_branch": "main",
                        "event": "push",
                        "path": f".github/workflows/{workflow}",
                        "status": "completed",
                        "conclusion": self.conclusion,
                    }
                ]
            }
        if self.ledger_state == "api_error":
            raise RuntimeError("raw-response-must-not-leak")
        deployment = {
            "id": 10,
            "created_at": "2026-09-01T00:00:00Z",
            "sha": "c" * 40,
            "environment": "db-migration",
            "task": "deploy:migrations",
            "payload": {
                "schema_version": 1,
                "release_sha": "c" * 40,
                "mode": "verify",
                "expected_start_revision": None,
                "target_revision": self.ledger_revision,
                "migration_tree_oid": self.ledger_tree,
                "github_run_id": 1,
                "github_run_attempt": 1,
                "baseline_deployment_id": None,
                "baseline_status_id": None,
            },
        }
        if path == "/deployments":
            return [] if self.ledger_state == "missing" else [deployment]
        if path == "/deployments/10":
            return deployment
        if path == "/deployments/10/statuses":
            return [
                {
                    "id": 20,
                    "created_at": "2026-09-01T00:00:01Z",
                    "state": self.ledger_state,
                }
            ]
        raise AssertionError(f"unexpected read: {path}")

    def post_json(self, *_args: object) -> object:
        raise AssertionError("app release must not write ledger")


class FakeRepository:
    def ensure_main(self, sha: str) -> None:
        assert sha == _SHA

    def schema(self, sha: str) -> SimpleNamespace:
        assert sha == _SHA
        return SimpleNamespace(head="r1", tree_oid=_TREE)


@pytest.mark.parametrize("check_schema", [False, True])
def test_current_main_accepts_applied_schema_even_when_ledger_sha_differs(
    check_schema: bool,
) -> None:
    result = guard.check_app_release(
        FakeRepository(),
        FakeGitHub(),
        release_sha=_SHA,
        check_schema=check_schema,
    )
    assert (result["result"], result["release_sha"]) == ("allowed", _SHA)


@pytest.mark.parametrize(
    "change",
    [
        {"main_sha": "d" * 40},
        {"conclusion": "failure"},
        {"ledger_state": "missing"},
        {"ledger_state": "failure"},
        {"ledger_state": "api_error"},
        {"ledger_revision": "r2"},
        {"ledger_tree": "e" * 40},
    ],
)
def test_unconfirmed_release_is_denied_without_repair(change: dict[str, str]) -> None:
    with pytest.raises(guard.ReleaseGuardError) as error:
        guard.check_app_release(
            FakeRepository(),
            FakeGitHub(**change),
            release_sha=_SHA,
            check_schema=True,
        )
    assert "raw-response" not in str(error.value)


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _release_fixture(tmp_path: Path) -> FakeGitHub:
    versions = tmp_path / "release/backend/alembic/versions"
    versions.mkdir(parents=True)
    (versions / "r1.py").write_text(
        "revision = 'r1'\ndown_revision = None\n"
        "raise RuntimeError('migration code must not execute')\n",
        encoding="utf-8",
    )
    git = shutil.which("git")
    assert git is not None

    def run(*args: str) -> str:
        return subprocess.check_output(  # noqa: S603
            [git, "-C", str(tmp_path / "release"), *args],
            text=True,
        ).strip()

    run("init", "--quiet")
    run("add", "backend")
    run(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    sha = run("rev-parse", "HEAD")
    run("update-ref", "refs/remotes/origin/main", sha)
    tree = run("rev-parse", f"{sha}:backend/alembic/versions")
    return FakeGitHub(release_sha=sha, main_sha=sha, tree_oid=tree, ledger_tree=tree)


@pytest.mark.parametrize(
    "change, registrations, updates",
    [
        ("none", 2, 2),
        ("approval_main", 0, 0),
        ("main", 2, 0),
        ("ledger", 2, 0),
    ],
)
def test_workflow_rechecks_before_any_service_update(
    tmp_path: Path,
    change: str,
    registrations: int,
    updates: int,
) -> None:
    github = _release_fixture(tmp_path)
    github.change_during_registration = change
    state = tmp_path / "github.json"
    events = tmp_path / "aws-events"
    state.write_text(json.dumps(asdict(github)))
    events.write_text("")
    scripts = tmp_path / ".rollout-control/.github/scripts"
    scripts.parent.mkdir(parents=True)
    scripts.symlink_to(_ROOT / ".github/scripts", target_is_directory=True)
    _executable(
        tmp_path / ".rollout-control/backend/.venv/bin/python",
        "import os, sys\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n",
    )
    _executable(
        tmp_path / "bin/gh",
        f"""
import json, os, sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_app_release_guard import FakeGitHub
if sys.argv[sys.argv.index('--method') + 1] != 'GET':
    raise SystemExit('unexpected write')
with open(os.environ['GUARD_FIXTURE']) as stream:
    github = FakeGitHub(**json.load(stream))
print(json.dumps(github.get_json(sys.argv[-1].removeprefix('repos/example/repo'))))
""",
    )
    _executable(
        tmp_path / "bin/aws",
        """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
operation = args[1]
events = Path(os.environ['AWS_EVENTS'])
with events.open('a') as stream:
    stream.write(operation + '\\n')
def argument(name):
    return args[args.index(name) + 1]
if operation == 'list-services':
    print('arn:ecs:service/vector/api\\tarn:ecs:service/vector/frontend')
elif operation == 'describe-services':
    print('arn:ecs:task-definition/vector-' + argument('--services') + ':1')
elif operation == 'describe-task-definition':
    family = argument('--task-definition')
    print(json.dumps({'family': family, 'containerDefinitions': [{
        'name': family.removeprefix('vector-'), 'image': 'example/app:old',
    }]}))
elif operation == 'register-task-definition':
    family = json.loads(argument('--cli-input-json'))['family']
    print('arn:ecs:task-definition/' + family + ':2')
    if events.read_text().splitlines().count('register-task-definition') == 2:
        path = Path(os.environ['GUARD_FIXTURE'])
        state = json.loads(path.read_text())
        change = state['change_during_registration']
        if change == 'main':
            state['main_sha'] = 'd' * 40
        if change == 'ledger':
            state['ledger_state'] = 'failure'
        path.write_text(json.dumps(state))
elif operation != 'update-service':
    raise SystemExit('unexpected AWS operation')
""",
    )
    with (_ROOT / ".github/workflows/aws-app-images.yml").open() as stream:
        workflow = yaml.safe_load(stream)
    steps = workflow["jobs"]["rollout"]["steps"]
    credential_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")
    )
    precheck = next(
        step["run"]
        for step in steps[:credential_index]
        if "check_app_release.py rollout" in step.get("run", "")
    )
    update = next(
        step["run"] for step in steps if "aws ecs update-service" in step.get("run", "")
    )
    if change == "approval_main":
        github.main_sha = "d" * 40
        state.write_text(json.dumps(asdict(github)))
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_REPOSITORY": "example/repo",
        "RELEASE_SHA": github.release_sha,
        "IMAGE_TAG": github.release_sha,
        "CLUSTER": "vector",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GUARD_FIXTURE": str(state),
        "AWS_EVENTS": str(events),
    }
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", precheck + "\n" + update],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    operations = events.read_text().splitlines()
    assert (
        result.returncode == 0,
        operations.count("register-task-definition"),
        operations.count("update-service"),
    ) == (change == "none", registrations, updates), result.stderr
