"""one-off ECS migration taskの実行・停止境界。"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "migration_ecs.py"
_SHA = "a" * 40
_ACCOUNT = "123456789012"
_BASE_TD = f"arn:aws:ecs:ap-northeast-1:{_ACCOUNT}:task-definition/vector-migration:4"
_EXPECTED_TD = (
    f"arn:aws:ecs:ap-northeast-1:{_ACCOUNT}:task-definition/vector-migration:5"
)
_TASK_ARN = f"arn:aws:ecs:ap-northeast-1:{_ACCOUNT}:task/vector/task-123"

pytestmark = pytest.mark.unit


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_ecs", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


migration = _load_module()


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _base_definition() -> dict[str, object]:
    return {
        "taskDefinition": {
            "taskDefinitionArn": _BASE_TD,
            "family": "vector-migration",
            "revision": 4,
            "status": "ACTIVE",
            "requiresCompatibilities": ["FARGATE"],
            "compatibilities": ["EC2", "FARGATE"],
            "networkMode": "awsvpc",
            "cpu": "256",
            "memory": "512",
            "registeredAt": "2026-09-01T00:00:00Z",
            "registeredBy": "arn:aws:iam::123456789012:role/vector-ci-terraform-apply",
            "requiresAttributes": [],
            "runtimePlatform": {
                "cpuArchitecture": "ARM64",
                "operatingSystemFamily": "LINUX",
            },
            "taskRoleArn": (
                "arn:aws:iam::123456789012:role/vector/vector-migration-task"
            ),
            "executionRoleArn": (
                "arn:aws:iam::123456789012:role/vector/vector-migration-exec"
            ),
            "containerDefinitions": [
                {
                    "name": "migration",
                    "image": (
                        "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/"
                        "vector/backend:old"
                    ),
                    "essential": True,
                    "privileged": False,
                    "command": [
                        "python",
                        "-m",
                        "scripts.migration_runner",
                    ],
                    "environment": [
                        {"name": "ENV", "value": "production"},
                        {"name": "AWS_REGION", "value": "ap-northeast-1"},
                        {"name": "DB_IAM_AUTH", "value": "true"},
                        {
                            "name": "MIGRATION_DATABASE_URL",
                            "value": "postgresql+asyncpg://vector@vector-db.example:5432/vector?sslmode=require",
                        },
                    ],
                    "secrets": [],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": "/ecs/vector/migration",
                            "awslogs-region": "ap-northeast-1",
                            "awslogs-stream-prefix": "ecs",
                        },
                    },
                }
            ],
        }
    }


@pytest.mark.parametrize("extra_query", ["password=leftover", "password="])
def test_base_definition_rejects_non_ssl_database_query(
    tmp_path: Path,
    extra_query: str,
) -> None:
    definition = _base_definition()["taskDefinition"]
    assert isinstance(definition, dict)
    containers = definition["containerDefinitions"]
    assert isinstance(containers, list)
    environment = containers[0]["environment"]
    database_url = next(
        item for item in environment if item["name"] == "MIGRATION_DATABASE_URL"
    )
    database_url["value"] = f"{database_url['value']}&{extra_query}"

    with pytest.raises(
        migration.MigrationInputError, match="base_database_query_mismatch"
    ):
        migration.prepare_task_definition(
            _config(tmp_path),
            {"taskDefinition": definition},
            command=["python", "-m", "scripts.migration_runner"],
        )


def _task(
    status: str, *, exit_code: int = 0, image_tag: str = _SHA
) -> dict[str, object]:
    container: dict[str, object] = {
        "name": "migration",
        "lastStatus": status,
        "image": (
            "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/"
            f"vector/backend:{image_tag}"
        ),
    }
    if status == "STOPPED":
        container["exitCode"] = exit_code
    return {
        "taskArn": _TASK_ARN,
        "taskDefinitionArn": _EXPECTED_TD,
        "lastStatus": status,
        "desiredStatus": "STOPPED" if status == "STOPPED" else "RUNNING",
        "startedBy": "vector-migration-987-2",
        "stopCode": "EssentialContainerExited" if status == "STOPPED" else None,
        "stoppedReason": "Essential container in task exited"
        if status == "STOPPED"
        else None,
        "containers": [container],
        "tags": [
            {"key": "VectorPurpose", "value": "migration"},
            {"key": "ReleaseSha", "value": _SHA},
            {"key": "GitHubRunId", "value": "987-2"},
        ],
    }


class FakeClient:
    def __init__(
        self,
        *,
        snapshots: Sequence[Mapping[str, object]] | None = None,
        existing: Sequence[str] = (),
        recovered: Sequence[str] = (),
        run_error: Exception | None = None,
    ) -> None:
        self.snapshots = list(snapshots or [_task("STOPPED")])
        self.existing = list(existing)
        self.recovered = list(recovered)
        self.run_error = run_error
        self.run_calls = 0
        self.stop_calls: list[str] = []
        self.registered: Mapping[str, object] | None = None

    def describe_task_definition(self, family: str) -> Mapping[str, object]:
        assert family == "vector-migration"
        return copy.deepcopy(_base_definition())

    def register_task_definition(
        self,
        definition: Mapping[str, object],
        tags: Mapping[str, str],
    ) -> Mapping[str, object]:
        self.registered = copy.deepcopy(definition)
        return {"taskDefinition": {"taskDefinitionArn": _EXPECTED_TD}, "tags": tags}

    def list_active_tasks(self, cluster: str, family: str) -> Sequence[str]:
        del cluster, family
        return list(self.existing)

    def list_started_tasks(
        self,
        cluster: str,
        family: str,
        started_by: str,
    ) -> Sequence[str]:
        del cluster, family, started_by
        return list(self.recovered)

    def describe_tasks(
        self, cluster: str, task_arns: Sequence[str]
    ) -> Mapping[str, object]:
        del cluster, task_arns
        index = min(self.run_calls - 1, len(self.snapshots) - 1)
        if self.run_calls == 0:
            index = 0
        payload = copy.deepcopy(self.snapshots[index])
        if self.run_calls > 0 and index < len(self.snapshots) - 1:
            self.run_calls += 1
        return {"tasks": [payload], "failures": []}

    def find_subnet_ids(self, name: str) -> Sequence[str]:
        return ["subnet-migration"] if name == "vector-migration" else []

    def find_security_group_ids(self, name: str) -> Sequence[str]:
        return ["sg-migration"] if name == "vector-migration" else []

    def run_task(self, **_kwargs: object) -> Mapping[str, object]:
        if self.run_error is not None:
            raise self.run_error
        self.run_calls = 1
        return {"tasks": [{"taskArn": _TASK_ARN}], "failures": []}

    def stop_task(self, cluster: str, task_arn: str, reason: str) -> None:
        del cluster, reason
        self.stop_calls.append(task_arn)
        self.run_calls = len(self.snapshots)


def _config(tmp_path: Path, *, timeout: float = 1200) -> object:
    return migration.MigrationConfig(
        cluster="vector",
        release_sha=_SHA,
        family="vector-migration",
        container_name="migration",
        network_name="vector-migration",
        started_by="vector-migration-987-2",
        github_run_id="987-2",
        state_file=tmp_path / "migration-state.json",
        summary_file=tmp_path / "summary.md",
        poll_seconds=15,
        timeout_seconds=timeout,
        cleanup_timeout_seconds=180,
    )


def test_pending_running_stopped_succeeds(tmp_path: Path) -> None:
    clock = FakeClock()
    client = FakeClient(
        snapshots=[
            _task("PENDING"),
            _task("RUNNING"),
            _task("DEPROVISIONING"),
            _task("STOPPED"),
        ]
    )
    control = migration.MigrationTaskControl(_config(tmp_path), client)
    state = control.start(_EXPECTED_TD, "subnet-migration", "sg-migration")
    result = control.wait(state, monotonic=clock.monotonic, sleep=clock.sleep)
    assert (result["lastStatus"], clock.sleeps) == ("STOPPED", [15, 15, 15])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda definition: definition["containerDefinitions"][0][
            "logConfiguration"
        ].update(logDriver="json-file"),
        lambda definition: definition["containerDefinitions"][0]["logConfiguration"][
            "options"
        ].update({"awslogs-stream-prefix": "forged`[link](x)"}),
        lambda definition: definition["containerDefinitions"][0]["logConfiguration"][
            "options"
        ].update({"awslogs-region": "us-east-1"}),
    ],
)
def test_base_log_configuration_drift_is_rejected(
    tmp_path: Path,
    mutate: object,
) -> None:
    definition = _base_definition()
    mutate(definition["taskDefinition"])  # type: ignore[operator,index]
    with pytest.raises(migration.MigrationInputError):
        migration.prepare_task_definition(
            _config(tmp_path),
            definition,
            command=["python", "-m", "scripts.migration_runner"],
        )


def test_cleanup_recovers_task_by_started_by_when_state_is_missing(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        recovered=[_TASK_ARN], snapshots=[_task("RUNNING"), _task("STOPPED")]
    )
    result = migration.cleanup_migration(_config(tmp_path), client)
    assert (result, client.stop_calls) == (migration.SUCCESS, [_TASK_ARN])


def test_cleanup_refuses_task_with_different_started_by(tmp_path: Path) -> None:
    task = _task("RUNNING")
    task["startedBy"] = "different-run"
    client = FakeClient(recovered=[_TASK_ARN], snapshots=[task])
    result = migration.cleanup_migration(_config(tmp_path), client)
    assert (result, client.stop_calls) == (migration.CONTRACT_FAILURE, [])


def test_state_file_is_written_immediately_after_run_task(tmp_path: Path) -> None:
    config = _config(tmp_path)
    migration.MigrationTaskControl(config, FakeClient()).start(
        _EXPECTED_TD, "subnet-migration", "sg-migration"
    )
    state = json.loads(config.state_file.read_text(encoding="utf-8"))
    assert state["task_arn"] == _TASK_ARN


def test_summary_redacts_arn_account_ip_env_and_log_body(tmp_path: Path) -> None:
    task = _task("STOPPED", exit_code=2)
    task["stoppedReason"] = (
        f"failed at 10.0.1.5 for {_TASK_ARN} {_ACCOUNT} "
        "MIGRATION_DATABASE_URL=postgresql+asyncpg://vector@vector-db.internal/db"
    )
    task["environment"] = [{"name": "RAW_ENV_SENTINEL", "value": "must-not-leak"}]
    task["logBody"] = "LOG_BODY_SENTINEL"
    config = _config(tmp_path)
    migration.cleanup_migration(
        config, FakeClient(recovered=[_TASK_ARN], snapshots=[task])
    )
    summary = config.summary_file.read_text(encoding="utf-8")
    assert not any(
        value in summary
        for value in (
            _TASK_ARN,
            _ACCOUNT,
            "10.0.1.5",
            "RAW_ENV_SENTINEL",
            "LOG_BODY_SENTINEL",
            "vector-db.internal",
            "MIGRATION_DATABASE_URL",
        )
    )
    assert "details withheld" in summary


def test_aws_cli_stderr_is_reduced_to_safe_error_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        "An error occurred (AccessDeniedException) when calling RunTask: "
        "MIGRATION_DATABASE_URL=postgresql+asyncpg://vector@vector-db.internal/db"
    )
    monkeypatch.setattr(
        migration.subprocess,
        "run",
        lambda *_args, **_kwargs: CompletedProcess([], 254, stdout="", stderr=stderr),
    )
    client = migration.AwsCliMigrationClient("/usr/bin/aws")

    with pytest.raises(migration.AwsCliError) as error:
        client._run("ecs", "run-task", "--cluster", "vector")

    rendered = str(error.value)
    assert rendered == (
        "AWS CLI ecs:run-task failed (exit=254, code=AccessDeniedException)"
    )
    assert "vector-db.internal" not in rendered


def test_run_task_explicitly_disables_execute_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> CompletedProcess[str]:
        captured.extend(arguments)
        return CompletedProcess(
            arguments,
            0,
            stdout=json.dumps({"tasks": [{"taskArn": _TASK_ARN}], "failures": []}),
            stderr="",
        )

    monkeypatch.setattr(migration.subprocess, "run", fake_run)
    client = migration.AwsCliMigrationClient("/usr/bin/aws")

    client.run_task(
        cluster="vector",
        task_definition=_EXPECTED_TD,
        subnet_id="subnet-migration",
        security_group_id="sg-migration",
        started_by="vector-migration-987-2",
        client_token="migration-987-2-token",
        tags={"VectorPurpose": "migration"},
    )

    assert "--disable-execute-command" in captured
    assert "--no-enable-execute-command" not in captured


def test_security_group_discovery_uses_ec2_describe_security_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> CompletedProcess[str]:
        captured.extend(arguments)
        return CompletedProcess(
            arguments,
            0,
            stdout=json.dumps({"SecurityGroups": [{"GroupId": "sg-migration"}]}),
            stderr="",
        )

    monkeypatch.setattr(migration.subprocess, "run", fake_run)
    client = migration.AwsCliMigrationClient("/usr/bin/aws")

    assert client.find_security_group_ids("vector-migration") == ["sg-migration"]
    assert captured[:3] == [
        "/usr/bin/aws",
        "ec2",
        "describe-security-groups",
    ]


def test_active_task_listing_includes_desired_stopped_until_last_status_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = migration.AwsCliMigrationClient("/usr/bin/aws")
    calls: list[tuple[str, ...]] = []

    def fake_run(*arguments: str) -> Mapping[str, object]:
        calls.append(arguments)
        if arguments[1] == "list-tasks":
            desired = (
                arguments[arguments.index("--desired-status") + 1]
                if "--desired-status" in arguments
                else "RUNNING"
            )
            return {"taskArns": [_TASK_ARN] if desired == "STOPPED" else []}
        return {
            "tasks": [
                {
                    "taskArn": _TASK_ARN,
                    "lastStatus": "STOPPING",
                    "startedBy": "vector-migration-987-2",
                }
            ],
            "failures": [],
        }

    monkeypatch.setattr(client, "_run", fake_run)

    assert client.list_active_tasks("vector", "vector-migration") == [_TASK_ARN]
    assert any("--desired-status" in call and "STOPPED" in call for call in calls)


def test_started_by_recovery_filters_completed_and_other_run_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = migration.AwsCliMigrationClient("/usr/bin/aws")
    other = _TASK_ARN.replace("task-123", "task-other")
    completed = _TASK_ARN.replace("task-123", "task-completed")

    def fake_run(*arguments: str) -> Mapping[str, object]:
        if arguments[1] == "list-tasks":
            if "--started-by" in arguments:
                return {"taskArns": []}
            return {"taskArns": [_TASK_ARN, other, completed]}
        return {
            "tasks": [
                {
                    "taskArn": _TASK_ARN,
                    "lastStatus": "STOPPING",
                    "startedBy": "vector-migration-987-2",
                },
                {
                    "taskArn": other,
                    "lastStatus": "STOPPING",
                    "startedBy": "vector-migration-other",
                },
                {
                    "taskArn": completed,
                    "lastStatus": "STOPPED",
                    "startedBy": "vector-migration-987-2",
                },
            ],
            "failures": [],
        }

    monkeypatch.setattr(client, "_run", fake_run)

    assert client.list_started_tasks(
        "vector",
        "vector-migration",
        "vector-migration-987-2",
    ) == [_TASK_ARN]


def test_markdown_encoder_neutralizes_table_and_link_syntax() -> None:
    rendered = migration._markdown("x|`[forged](https://example.invalid)\nnext")
    assert "|" not in rendered
    assert "`" not in rendered
    assert "[" not in rendered
    assert "]" not in rendered
    assert "\n" not in rendered
