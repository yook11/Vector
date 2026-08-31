"""ECS rollout検証器のruntime観測契約。"""

from __future__ import annotations

import copy
import importlib.util
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "verify_ecs_rollout.py"
_SHA = "a" * 40
_STARTED_AT = datetime(2026, 8, 31, 5, 41, tzinfo=UTC)
_SERVICE = "agent"
_EXPECTED_TD = "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/vector-agent:18"
_API_SERVICE = "api"
_API_EXPECTED_TD = (
    "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/vector-api:20"
)

pytestmark = pytest.mark.unit


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_ecs_rollout", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rollout = _load_module()


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeEcsClient:
    def __init__(
        self,
        *,
        snapshots: Sequence[Mapping[str, object]],
        running_tasks: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        stopped_tasks: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        describe_services_error: Exception | None = None,
        list_task_errors: Mapping[tuple[str, str], Exception] | None = None,
        describe_task_failures: Sequence[Mapping[str, object]] = (),
    ) -> None:
        self._snapshots = list(snapshots)
        self._snapshot_index = 0
        self._running_tasks = {
            key: list(value) for key, value in (running_tasks or {}).items()
        }
        self._stopped_tasks = {
            key: list(value) for key, value in (stopped_tasks or {}).items()
        }
        self._describe_services_error = describe_services_error
        self._list_task_errors = dict(list_task_errors or {})
        self._describe_task_failures = list(describe_task_failures)

    def describe_services(
        self, cluster: str, services: Sequence[str]
    ) -> Mapping[str, object]:
        del cluster, services
        if self._describe_services_error is not None:
            raise self._describe_services_error
        index = min(self._snapshot_index, len(self._snapshots) - 1)
        self._snapshot_index += 1
        return copy.deepcopy(self._snapshots[index])

    def list_tasks(
        self, cluster: str, service: str, desired_status: str
    ) -> Sequence[str]:
        del cluster
        error = self._list_task_errors.get((service, desired_status))
        if error is not None:
            raise error
        tasks = (
            self._running_tasks if desired_status == "RUNNING" else self._stopped_tasks
        )
        return [str(task["taskArn"]) for task in tasks.get(service, [])]

    def describe_tasks(
        self, cluster: str, task_arns: Sequence[str]
    ) -> Mapping[str, object]:
        del cluster
        all_tasks = [
            *(
                task
                for service_tasks in self._running_tasks.values()
                for task in service_tasks
            ),
            *(
                task
                for service_tasks in self._stopped_tasks.values()
                for task in service_tasks
            ),
        ]
        selected = [task for task in all_tasks if task.get("taskArn") in task_arns]
        return {
            "tasks": copy.deepcopy(selected),
            "failures": copy.deepcopy(self._describe_task_failures),
        }

    def describe_task_definition(self, task_definition: str) -> Mapping[str, object]:
        service = _API_SERVICE if "vector-api" in task_definition else _SERVICE
        return {
            "taskDefinition": {
                "containerDefinitions": [
                    {
                        "name": service,
                        "logConfiguration": {
                            "options": {
                                "awslogs-group": f"/ecs/vector/{service}",
                                "awslogs-stream-prefix": "ecs",
                            }
                        },
                        "environment": [
                            {"name": "RAW_ENV_SENTINEL", "value": "must-not-leak"}
                        ],
                        "secrets": [
                            {
                                "name": "RAW_SECRET_SENTINEL",
                                "valueFrom": "raw-secret-reference-must-not-leak",
                            }
                        ],
                    }
                ]
            }
        }


def _service(
    state: str,
    *,
    name: str = _SERVICE,
    expected_td: str = _EXPECTED_TD,
    current_td: str | None = None,
    primary_td: str | None = None,
    desired: int = 1,
    running: int = 1,
    pending: int = 0,
    old_running: int = 0,
    old_pending: int = 0,
    reason: str = "deployment completed",
    events: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    current_td = current_td or expected_td
    primary_td = primary_td or expected_td
    return {
        "serviceName": name,
        "taskDefinition": current_td,
        "desiredCount": desired,
        "runningCount": running,
        "pendingCount": pending,
        "deployments": [
            {
                "status": "PRIMARY",
                "taskDefinition": primary_td,
                "rolloutState": state,
                "rolloutStateReason": reason,
                "desiredCount": desired,
                "runningCount": running,
                "pendingCount": pending,
                "failedTasks": 0,
            },
            {
                "status": "ACTIVE",
                "taskDefinition": "old-task-definition",
                "runningCount": old_running,
                "pendingCount": old_pending,
            },
        ],
        "events": list(events),
    }


def _snapshot(service: Mapping[str, object]) -> dict[str, object]:
    return {"services": [service], "failures": []}


def _running_task(
    *,
    task_id: str = "running-task",
    task_definition: str = _EXPECTED_TD,
    task_status: str = "RUNNING",
    container_name: str = _SERVICE,
    container_status: str = "RUNNING",
    image_tag: str = _SHA,
) -> dict[str, object]:
    return {
        "taskArn": f"arn:aws:ecs:ap-northeast-1:123456789012:task/vector/{task_id}",
        "taskDefinitionArn": task_definition,
        "lastStatus": task_status,
        "createdAt": "2026-08-31T05:42:00+00:00",
        "containers": [
            {
                "name": container_name,
                "lastStatus": container_status,
                "image": (
                    "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/"
                    f"vector/backend:{image_tag}"
                ),
            }
        ],
    }


def _stopped_task(
    task_id: str,
    *,
    stopped_at: str,
    task_definition: str = _EXPECTED_TD,
) -> dict[str, object]:
    return {
        "taskArn": f"arn:aws:ecs:ap-northeast-1:123456789012:task/vector/{task_id}",
        "taskDefinitionArn": task_definition,
        "createdAt": stopped_at,
        "stoppedAt": stopped_at,
        "stopCode": "EssentialContainerExited",
        "stoppedReason": (
            "task in account 123456789012 stopped at private addresses "
            "10.0.1.24 and fd00::1 using "
            "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:prod-db "
            "and arn:aws-iso-b:secretsmanager:us-isob-east-1:123456789012:"
            "secret:iso-prod-db"
        ),
        "containers": [
            {
                "name": _SERVICE,
                "lastStatus": "STOPPED",
                "exitCode": 1,
                "reason": (
                    "container from "
                    "arn:aws:ecs:ap-northeast-1:123456789012:task/x failed"
                ),
                "image": f"registry/backend:{_SHA}",
            }
        ],
    }


def _config(
    tmp_path: Path,
    *,
    timeout: float = 30,
    expected_task_definitions: Mapping[str, str] | None = None,
) -> object:
    return rollout.VerificationConfig(
        cluster="vector",
        image_tag=_SHA,
        expected_task_definitions=(
            expected_task_definitions or {_SERVICE: _EXPECTED_TD}
        ),
        rollout_started_at=_STARTED_AT,
        timeout_seconds=timeout,
        poll_seconds=15,
        summary_file=tmp_path / "summary.md",
    )


def _verify(
    tmp_path: Path,
    client: FakeEcsClient,
    *,
    timeout: float = 30,
    expected_task_definitions: Mapping[str, str] | None = None,
) -> tuple[int, str, list[float]]:
    clock = FakeClock()
    code = rollout.verify_rollout(
        _config(
            tmp_path,
            timeout=timeout,
            expected_task_definitions=expected_task_definitions,
        ),
        client,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    return (
        code,
        (tmp_path / "summary.md").read_text(encoding="utf-8"),
        clock.sleeps,
    )


def test_completed_rollout_accepts_running_task_image(tmp_path: Path) -> None:
    client = FakeEcsClient(
        snapshots=[_snapshot(_service("COMPLETED"))],
        running_tasks={_SERVICE: [_running_task()]},
    )

    code, summary, sleeps = _verify(tmp_path, client)

    assert (code, "Result: **success**" in summary, _SHA in summary, sleeps) == (
        rollout.SUCCESS,
        True,
        True,
        [],
    )


def test_in_progress_rollout_polls_until_completed(tmp_path: Path) -> None:
    client = FakeEcsClient(
        snapshots=[
            _snapshot(_service("IN_PROGRESS")),
            _snapshot(_service("COMPLETED")),
        ],
        running_tasks={_SERVICE: [_running_task()]},
    )

    code, _, sleeps = _verify(tmp_path, client)

    assert (code, sleeps) == (rollout.SUCCESS, [15])


def test_failed_primary_stops_without_sleep(tmp_path: Path) -> None:
    client = FakeEcsClient(
        snapshots=[
            _snapshot(
                _service(
                    "FAILED",
                    reason="service deployment failed",
                    events=[
                        {
                            "createdAt": "2026-08-31T05:43:00+00:00",
                            "message": "unable to start task",
                        }
                    ],
                )
            )
        ],
        running_tasks={_SERVICE: [_running_task()]},
    )

    code, summary, sleeps = _verify(tmp_path, client)

    assert (
        code,
        sleeps,
        "PRIMARY rollout failed" in summary,
        "service deployment failed" in summary,
    ) == (rollout.ROLLOUT_FAILURE, [], True, True)


def test_failed_primary_keeps_other_service_snapshot(tmp_path: Path) -> None:
    api_task = _running_task(task_id="api-running", task_definition=_API_EXPECTED_TD)
    api_task["containers"][0]["name"] = _API_SERVICE  # type: ignore[index]
    client = FakeEcsClient(
        snapshots=[
            {
                "services": [
                    _service("FAILED", reason="agent deployment failed"),
                    _service(
                        "IN_PROGRESS",
                        name=_API_SERVICE,
                        expected_td=_API_EXPECTED_TD,
                        reason="api deployment in progress",
                    ),
                ],
                "failures": [],
            }
        ],
        running_tasks={
            _SERVICE: [_running_task()],
            _API_SERVICE: [api_task],
        },
    )

    code, summary, sleeps = _verify(
        tmp_path,
        client,
        expected_task_definitions={
            _SERVICE: _EXPECTED_TD,
            _API_SERVICE: _API_EXPECTED_TD,
        },
    )

    assert (
        code,
        sleeps,
        "agent deployment failed" in summary,
        "api deployment in progress" in summary,
        "ecs/api/api-running" in summary,
    ) == (rollout.ROLLOUT_FAILURE, [], True, True, True)


def test_in_progress_rollout_times_out_with_diagnostics(tmp_path: Path) -> None:
    client = FakeEcsClient(
        snapshots=[_snapshot(_service("IN_PROGRESS"))],
        running_tasks={_SERVICE: [_running_task()]},
    )

    code, summary, sleeps = _verify(tmp_path, client, timeout=30)

    assert (
        code,
        sleeps,
        "rollout_timeout" in summary,
        "ECS rollout verification timed out" in summary,
    ) == (rollout.ROLLOUT_FAILURE, [15, 15], True, True)


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("service_td", "service_task_definition_mismatch"),
        ("primary_td", "primary_task_definition_mismatch"),
        ("running_count", "running_count_mismatch"),
        ("pending", "pending_tasks_remaining"),
        ("old_deployment", "old_deployment_active"),
        ("task_count", "running_task_count_mismatch"),
        ("task_td", "running_task_definition_mismatch"),
        ("task_status", "running_task_status_mismatch"),
        ("container_missing", "service_container_count_mismatch"),
        ("container_status", "service_container_not_running"),
        ("image_tag", "running_container_image_tag_mismatch"),
    ],
)
def test_completed_rollout_rejects_postcondition_violation(
    tmp_path: Path,
    scenario: str,
    reason: str,
) -> None:
    service = _service("COMPLETED")
    task = _running_task()
    tasks = [task]
    if scenario == "service_td":
        service["taskDefinition"] = "unexpected"
    elif scenario == "primary_td":
        service["deployments"][0]["taskDefinition"] = "unexpected"  # type: ignore[index]
    elif scenario == "running_count":
        service["runningCount"] = 0
    elif scenario == "pending":
        service["pendingCount"] = 1
    elif scenario == "old_deployment":
        service["deployments"][1]["runningCount"] = 1  # type: ignore[index]
    elif scenario == "task_count":
        tasks = []
    elif scenario == "task_td":
        task["taskDefinitionArn"] = "unexpected"
    elif scenario == "task_status":
        task["lastStatus"] = "PENDING"
    elif scenario == "container_missing":
        task["containers"][0]["name"] = "unexpected"  # type: ignore[index]
    elif scenario == "container_status":
        task["containers"][0]["lastStatus"] = "STOPPED"  # type: ignore[index]
    elif scenario == "image_tag":
        task["containers"][0]["image"] = "registry/backend:wrong"  # type: ignore[index]
    client = FakeEcsClient(
        snapshots=[_snapshot(service)],
        running_tasks={_SERVICE: tasks},
    )

    code, summary, sleeps = _verify(tmp_path, client)

    assert (code, reason in summary, sleeps) == (rollout.ROLLOUT_FAILURE, True, [])


def test_runtime_api_error_keeps_other_service_snapshot(tmp_path: Path) -> None:
    api_task = _running_task(
        task_id="api-running",
        task_definition=_API_EXPECTED_TD,
        container_name=_API_SERVICE,
    )
    client = FakeEcsClient(
        snapshots=[
            {
                "services": [
                    _service("COMPLETED"),
                    _service(
                        "COMPLETED",
                        name=_API_SERVICE,
                        expected_td=_API_EXPECTED_TD,
                    ),
                ],
                "failures": [],
            }
        ],
        running_tasks={_API_SERVICE: [api_task]},
        list_task_errors={
            (_SERVICE, "RUNNING"): rollout.AwsCliError("agent task API failed")
        },
    )

    code, summary, sleeps = _verify(
        tmp_path,
        client,
        expected_task_definitions={
            _SERVICE: _EXPECTED_TD,
            _API_SERVICE: _API_EXPECTED_TD,
        },
    )

    assert (
        code,
        sleeps,
        "agent task API failed" in summary,
        "ECS runtime observation failed" in summary,
        _API_SERVICE in summary,
        _SHA in summary,
    ) == (rollout.CONTRACT_FAILURE, [], True, True, True, True)


def test_describe_task_failure_is_contract_failure(tmp_path: Path) -> None:
    client = FakeEcsClient(
        snapshots=[_snapshot(_service("COMPLETED"))],
        running_tasks={_SERVICE: [_running_task()]},
        describe_task_failures=[{"arn": "task/failure", "reason": "MISSING"}],
    )

    code, summary, sleeps = _verify(tmp_path, client)

    assert (code, sleeps, "running_task_describe_failure" in summary) == (
        rollout.CONTRACT_FAILURE,
        [],
        True,
    )


def test_failure_diagnostics_filter_tasks_and_redact_public_summary(
    tmp_path: Path,
) -> None:
    stopped_tasks = [
        _stopped_task("before-rollout", stopped_at="2026-08-31T05:40:00+00:00"),
        _stopped_task("stopped-1", stopped_at="2026-08-31T05:42:00+00:00"),
        _stopped_task("stopped-2", stopped_at="2026-08-31T05:43:00+00:00"),
        _stopped_task("stopped-3", stopped_at="2026-08-31T05:44:00+00:00"),
        _stopped_task("stopped-4", stopped_at="2026-08-31T05:45:00+00:00"),
        _stopped_task(
            "other-td",
            stopped_at="2026-08-31T05:46:00+00:00",
            task_definition="other",
        ),
    ]
    client = FakeEcsClient(
        snapshots=[
            _snapshot(
                _service(
                    "FAILED",
                    reason="target 10.0.1.24 in account 123456789012 failed",
                    events=[
                        {
                            "createdAt": "2026-08-31T05:45:00+00:00",
                            "message": (
                                "arn:aws:ecs:ap-northeast-1:123456789012:task/"
                                "vector/event-task failed at 10.0.1.24"
                            ),
                        }
                    ],
                )
            )
        ],
        running_tasks={_SERVICE: [_running_task()]},
        stopped_tasks={_SERVICE: stopped_tasks},
    )

    code, summary, _ = _verify(tmp_path, client)

    assert (
        code,
        all(task_id in summary for task_id in ("stopped-2", "stopped-3", "stopped-4")),
        all(
            task_id not in summary
            for task_id in ("before-rollout", "stopped-1", "other-td")
        ),
        "123456789012" not in summary,
        "10.0.1.24" not in summary,
        "arn:aws" not in summary,
        "fd00::1" not in summary,
        "prod-db" not in summary,
        "iso-prod-db" not in summary,
        "must-not-leak" not in summary,
        "raw-secret-reference-must-not-leak" not in summary,
        "&lt;ARN&gt;" in summary,
        "&lt;PRIVATE_IP&gt;" in summary,
        "/ecs/vector/agent" in summary,
        "ecs/agent/stopped-4" in summary,
        "old-task-definition" in summary,
        "ACTIVE" in summary,
    ) == (
        rollout.ROLLOUT_FAILURE,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    )


def test_missing_primary_is_contract_failure(tmp_path: Path) -> None:
    service = _service("COMPLETED")
    service["deployments"] = []
    client = FakeEcsClient(snapshots=[_snapshot(service)])

    code, summary, sleeps = _verify(tmp_path, client)

    assert (code, "primary_deployment_count=0" in summary, sleeps) == (
        rollout.CONTRACT_FAILURE,
        True,
        [],
    )


def test_aws_api_failure_is_contract_failure(tmp_path: Path) -> None:
    client = FakeEcsClient(
        snapshots=[_snapshot(_service("COMPLETED"))],
        describe_services_error=rollout.AwsCliError("describe failed"),
    )

    code, summary, sleeps = _verify(tmp_path, client)

    assert (code, "AWS API error" in summary, "describe failed" in summary, sleeps) == (
        rollout.CONTRACT_FAILURE,
        True,
        True,
        [],
    )
