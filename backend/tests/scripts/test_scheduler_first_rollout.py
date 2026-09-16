"""APP workflowのscheduler先行更新を外部通信なしで検証する。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/aws-app-images.yml"
pytestmark = pytest.mark.unit


def _run_rollout(
    tmp_path: Path,
    *,
    definitions: dict[str, str] | None = None,
    verify_exit: int = 0,
    guard_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(
        s for s in workflow["jobs"]["rollout"]["steps"] if s.get("id") == "rollout"
    )
    script = step["run"]
    script = (
        "set -euo pipefail\n" + script[script.index('expected_file="$RUNNER_TEMP/') :]
    )
    definitions = (
        definitions
        if definitions is not None
        else {
            "analysis": "vector-analysis:55",
            "api": "vector-api:12",
            "scheduler": "vector-scheduler:21",
        }
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    stub = (
        f"#!{sys.executable}\n"
        + """
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
name = Path(sys.argv[0]).name
status = 0
if name == "aws":
    assert args[:2] == ["ecs", "update-service"]
    event = {"action": "update", "service": args[args.index("--service") + 1]}
elif name == "python3":
    file = Path(args[args.index("--expected-task-definitions") + 1])
    expected = json.loads(file.read_text())
    event = {"action": "verify", "definitions": expected}
    status = int(os.environ["VERIFY_EXIT"])
else:
    event = {"action": "guard"}
    status = int(os.environ["GUARD_EXIT"])
with Path(os.environ["CALLS_FILE"]).open("a") as output:
    output.write(json.dumps(event) + "\\n")
sys.exit(status)
"""
    )
    guard = tmp_path / ".rollout-control/backend/.venv/bin/python"
    guard.parent.mkdir(parents=True)
    for executable in (bin_dir / "aws", bin_dir / "python3", guard):
        executable.write_text(stub)
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_REPOSITORY": "example/vector",
        "CLUSTER": "vector",
        "IMAGE_TAG": "a" * 40,
        "RELEASE_SHA": "a" * 40,
        "expected_task_definitions": json.dumps(definitions),
        "registered": "\n".join(f"{name} {arn}" for name, arn in definitions.items()),
        "CALLS_FILE": str(calls),
        "VERIFY_EXIT": str(verify_exit),
        "GUARD_EXIT": str(guard_exit),
    }
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    events = (
        [json.loads(line) for line in calls.read_text().splitlines()]
        if calls.exists()
        else []
    )
    return result, events


def test_scheduler_converges_before_other_services_are_updated(tmp_path: Path) -> None:
    """サービス一覧の順序にかかわらずschedulerを一度だけ先行更新する。"""
    result, events = _run_rollout(tmp_path)
    assert result.returncode == 0, result.stderr
    assert events == [
        {"action": "update", "service": "scheduler"},
        {"action": "verify", "definitions": {"scheduler": "vector-scheduler:21"}},
        {"action": "guard"},
        {"action": "update", "service": "analysis"},
        {"action": "update", "service": "api"},
    ]


@pytest.mark.parametrize(
    "verify_exit", [1, 2], ids=["rollout-failure-or-timeout", "contract-error"]
)
def test_scheduler_verification_failure_prevents_worker_updates(
    tmp_path: Path, verify_exit: int
) -> None:
    """schedulerが収束しない場合はworkerへの更新を送信しない。"""
    result, events = _run_rollout(tmp_path, verify_exit=verify_exit)
    assert result.returncode == verify_exit
    assert [e for e in events if e["action"] == "update"] == [
        {"action": "update", "service": "scheduler"}
    ]


def test_missing_scheduler_prevents_all_updates(tmp_path: Path) -> None:
    """schedulerを特定できない場合は他サービスも更新しない。"""
    result, events = _run_rollout(
        tmp_path, definitions={"analysis": "vector-analysis:55"}
    )
    assert result.returncode != 0
    assert events == []


def test_release_guard_failure_after_wait_prevents_worker_updates(
    tmp_path: Path,
) -> None:
    """scheduler待機中にrelease条件が変わった場合はworkerの更新を止める。"""
    result, events = _run_rollout(tmp_path, guard_exit=1)
    assert result.returncode == 1
    assert [e for e in events if e["action"] == "update"] == [
        {"action": "update", "service": "scheduler"}
    ]
