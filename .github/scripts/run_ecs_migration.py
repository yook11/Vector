"""release SHA固定のone-off ECS migration taskを実行・検証・停止する。"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

SUCCESS = 0
MIGRATION_FAILURE = 1
CONTRACT_FAILURE = 2

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_STARTED_BY_RE = re.compile(r"^[A-Za-z0-9_/-]{1,128}$")
_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_ARN_RE = re.compile(r"arn:[a-z0-9-]+:[^\s|,;)]+")
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_AWS_ERROR_CODE_RE = re.compile(r"An error occurred \(([A-Za-z0-9._-]{1,128})\)")
_SAFE_STOP_REASON_PREFIXES = (
    "Essential container in task exited",
    "Task failed to start",
    "CannotPullContainerError",
    "ResourceInitializationError",
    "OutOfMemoryError",
    "Task stopped by user",
    "GitHub Actions migration cleanup",
)
_READ_ONLY_TASK_DEFINITION_FIELDS = frozenset(
    {
        "taskDefinitionArn",
        "revision",
        "status",
        "requiresAttributes",
        "compatibilities",
        "registeredAt",
        "registeredBy",
        "deregisteredAt",
    }
)


class MigrationInputError(ValueError):
    """migration control-planeの入力・AWS response契約違反。"""


class AwsCliError(RuntimeError):
    """AWS CLI呼び出し失敗。"""


class MigrationTaskFailed(RuntimeError):
    """taskの起動・終了条件違反。"""


class MigrationTaskTimedOut(MigrationTaskFailed):
    """taskが期限までにSTOPPEDへ到達しなかった。"""


class MigrationClient(Protocol):
    def describe_task_definition(self, family: str) -> Mapping[str, object]: ...

    def register_task_definition(
        self,
        definition: Mapping[str, object],
        tags: Mapping[str, str],
    ) -> Mapping[str, object]: ...

    def list_active_tasks(self, cluster: str, family: str) -> Sequence[str]: ...

    def list_started_tasks(
        self,
        cluster: str,
        family: str,
        started_by: str,
    ) -> Sequence[str]: ...

    def describe_tasks(
        self,
        cluster: str,
        task_arns: Sequence[str],
    ) -> Mapping[str, object]: ...

    def find_subnet_ids(self, name: str) -> Sequence[str]: ...

    def find_security_group_ids(self, name: str) -> Sequence[str]: ...

    def run_task(self, **kwargs: object) -> Mapping[str, object]: ...

    def stop_task(self, cluster: str, task_arn: str, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class MigrationConfig:
    cluster: str
    release_sha: str
    family: str
    container_name: str
    network_name: str
    started_by: str
    github_run_id: str
    state_file: Path
    summary_file: Path
    poll_seconds: float
    timeout_seconds: float
    cleanup_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class MigrationState:
    task_arn: str
    task_definition_arn: str
    started_by: str

    def as_dict(self) -> dict[str, str]:
        return {
            "task_arn": self.task_arn,
            "task_definition_arn": self.task_definition_arn,
            "started_by": self.started_by,
        }


class AwsCliMigrationClient:
    """shellを使わずAWS CLI JSONだけを扱うcontrol-plane adapter。"""

    def __init__(self, aws_path: str) -> None:
        self._aws_path = aws_path

    def _run(self, *arguments: str) -> Mapping[str, object]:
        completed = subprocess.run(  # noqa: S603
            [self._aws_path, *arguments, "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            operation = _safe_aws_operation(arguments)
            match = _AWS_ERROR_CODE_RE.search(completed.stderr)
            error_code = match.group(1) if match is not None else "unknown"
            raise AwsCliError(
                f"AWS CLI {operation} failed "
                f"(exit={completed.returncode}, code={error_code})"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AwsCliError("AWS CLI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AwsCliError("AWS CLI response must be a JSON object")
        return payload

    def describe_task_definition(self, family: str) -> Mapping[str, object]:
        return self._run(
            "ecs",
            "describe-task-definition",
            "--task-definition",
            family,
            "--include",
            "TAGS",
        )

    def register_task_definition(
        self,
        definition: Mapping[str, object],
        tags: Mapping[str, str],
    ) -> Mapping[str, object]:
        tag_arguments = [f"key={key},value={value}" for key, value in tags.items()]
        return self._run(
            "ecs",
            "register-task-definition",
            "--cli-input-json",
            json.dumps(definition, separators=(",", ":")),
            "--tags",
            *tag_arguments,
        )

    def list_active_tasks(self, cluster: str, family: str) -> Sequence[str]:
        running = self._list_tasks(
            cluster,
            "--family",
            family,
            "--desired-status",
            "RUNNING",
        )
        stopping = self._list_tasks(
            cluster,
            "--family",
            family,
            "--desired-status",
            "STOPPED",
        )
        return self._active_candidates(cluster, [*running, *stopping])

    def list_started_tasks(
        self,
        cluster: str,
        family: str,
        started_by: str,
    ) -> Sequence[str]:
        running = self._list_tasks(cluster, "--started-by", started_by)
        stopping = self._list_tasks(
            cluster,
            "--family",
            family,
            "--desired-status",
            "STOPPED",
        )
        return self._active_candidates(
            cluster,
            [*running, *stopping],
            started_by=started_by,
        )

    def _list_tasks(self, cluster: str, *filters: str) -> list[str]:
        payload = self._run(
            "ecs",
            "list-tasks",
            "--cluster",
            cluster,
            *filters,
        )
        return _string_list(payload, "taskArns", "list-tasks")

    def _active_candidates(
        self,
        cluster: str,
        task_arns: Sequence[str],
        *,
        started_by: str | None = None,
    ) -> list[str]:
        candidates = list(dict.fromkeys(task_arns))
        active: list[str] = []
        for offset in range(0, len(candidates), 100):
            chunk = candidates[offset : offset + 100]
            response = self.describe_tasks(cluster, chunk)
            if _mapping_items(response.get("failures")):
                raise AwsCliError("DescribeTasks returned failures")
            tasks = _mapping_items(response.get("tasks"))
            described = {str(task.get("taskArn", "")) for task in tasks}
            if described != set(chunk):
                raise AwsCliError("DescribeTasks omitted migration task candidates")
            for task in tasks:
                if str(task.get("lastStatus", "")) == "STOPPED":
                    continue
                if started_by is not None and task.get("startedBy") != started_by:
                    continue
                active.append(str(task["taskArn"]))
        return active

    def describe_tasks(
        self,
        cluster: str,
        task_arns: Sequence[str],
    ) -> Mapping[str, object]:
        if not task_arns:
            return {"tasks": [], "failures": []}
        return self._run(
            "ecs",
            "describe-tasks",
            "--cluster",
            cluster,
            "--tasks",
            *task_arns,
            "--include",
            "TAGS",
        )

    def find_subnet_ids(self, name: str) -> Sequence[str]:
        payload = self._run(
            "ec2",
            "describe-subnets",
            "--filters",
            f"Name=tag:Name,Values={name}",
            "Name=state,Values=available",
        )
        return [
            str(item.get("SubnetId", ""))
            for item in _mapping_items(payload.get("Subnets"))
            if item.get("SubnetId")
        ]

    def find_security_group_ids(self, name: str) -> Sequence[str]:
        payload = self._run(
            "ec2",
            "describe-security-groups",
            "--filters",
            f"Name=tag:Name,Values={name}",
        )
        return [
            str(item.get("GroupId", ""))
            for item in _mapping_items(payload.get("SecurityGroups"))
            if item.get("GroupId")
        ]

    def run_task(self, **kwargs: object) -> Mapping[str, object]:
        tags = kwargs["tags"]
        if not isinstance(tags, Mapping):
            raise MigrationInputError("run-task tags must be a mapping")
        tag_arguments = [f"key={key},value={value}" for key, value in tags.items()]
        network = {
            "awsvpcConfiguration": {
                "subnets": [str(kwargs["subnet_id"])],
                "securityGroups": [str(kwargs["security_group_id"])],
                "assignPublicIp": "DISABLED",
            }
        }
        return self._run(
            "ecs",
            "run-task",
            "--cluster",
            str(kwargs["cluster"]),
            "--task-definition",
            str(kwargs["task_definition"]),
            "--launch-type",
            "FARGATE",
            "--network-configuration",
            json.dumps(network, separators=(",", ":")),
            "--started-by",
            str(kwargs["started_by"]),
            "--client-token",
            str(kwargs["client_token"]),
            "--disable-execute-command",
            "--tags",
            *tag_arguments,
        )

    def stop_task(self, cluster: str, task_arn: str, reason: str) -> None:
        self._run(
            "ecs",
            "stop-task",
            "--cluster",
            cluster,
            "--task",
            task_arn,
            "--reason",
            reason,
        )


def run_migration(
    config: MigrationConfig,
    client: MigrationClient,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """残存taskを拒否し、新規taskのSTOPPED後条件まで検証する。"""
    task_arn: str | None = None
    task_definition_arn = ""
    log_group = ""
    log_prefix = "ecs"
    try:
        _validate_config(config)
        existing = list(client.list_active_tasks(config.cluster, config.family))
        if existing:
            task_ids = ", ".join(_resource_suffix(value) for value in existing)
            raise MigrationTaskFailed(f"active migration task remains: {task_ids}")

        subnet_id = _one(client.find_subnet_ids(config.network_name), "subnet")
        security_group_id = _one(
            client.find_security_group_ids(config.network_name),
            "security group",
        )
        source = client.describe_task_definition(config.family)
        definition, log_group, log_prefix = _registration_for_release(config, source)
        tags = _task_tags(config)
        registered = client.register_task_definition(definition, tags)
        task_definition_arn = _registered_task_definition_arn(registered, config)
        response = client.run_task(
            cluster=config.cluster,
            task_definition=task_definition_arn,
            subnet_id=subnet_id,
            security_group_id=security_group_id,
            started_by=config.started_by,
            client_token=_client_token(config),
            tags=tags,
        )
        task_arn = _started_task_arn(response)
        _write_state(
            config.state_file,
            MigrationState(task_arn, task_definition_arn, config.started_by),
        )
        task = _wait_for_stopped(
            config,
            client,
            task_arn,
            timeout_seconds=config.timeout_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
        issues = _runtime_issues(config, task_definition_arn, task)
        if issues:
            raise MigrationTaskFailed(", ".join(issues))
        _write_summary(
            config,
            result="success",
            task=task,
            task_definition_arn=task_definition_arn,
            log_group=log_group,
            log_prefix=log_prefix,
        )
        return SUCCESS
    except (MigrationInputError, AwsCliError) as exc:
        code = CONTRACT_FAILURE
        reason = str(exc)
    except MigrationTaskFailed as exc:
        code = MIGRATION_FAILURE
        reason = str(exc)
    except Exception as exc:
        code = CONTRACT_FAILURE
        reason = f"unexpected controller failure: {type(exc).__name__}"

    cleanup_reason = ""
    task: Mapping[str, object] = {}
    if task_arn is not None:
        try:
            task = _stop_and_wait(
                config,
                client,
                task_arn,
                monotonic=monotonic,
                sleep=sleep,
            )
        except Exception as exc:
            cleanup_reason = f"cleanup failed: {type(exc).__name__}"
            code = CONTRACT_FAILURE
    _write_summary(
        config,
        result="failure",
        reason=", ".join(value for value in (reason, cleanup_reason) if value),
        task=task,
        task_definition_arn=task_definition_arn,
        log_group=log_group,
        log_prefix=log_prefix,
    )
    print(f"::error::{_sanitize(reason)}")
    return code


def cleanup_migration(
    config: MigrationConfig,
    client: MigrationClient,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """stateのexact ARN、欠落時は同じstartedByのtaskだけを停止する。"""
    try:
        _validate_config(config)
        state = _read_state(config.state_file)
        if state is not None:
            task_arn = state.task_arn
        else:
            recovered = list(
                client.list_started_tasks(
                    config.cluster,
                    config.family,
                    config.started_by,
                )
            )
            if not recovered:
                return SUCCESS
            task_arn = _one(recovered, "startedBy task")
        task = _describe_one_task(client, config.cluster, task_arn)
        _validate_cleanup_target(config, task_arn, task)
        if str(task.get("lastStatus", "")) != "STOPPED":
            task = _stop_and_wait(
                config,
                client,
                task_arn,
                monotonic=monotonic,
                sleep=sleep,
            )
        _write_summary(config, result="cleanup-completed", task=task)
        return SUCCESS
    except (MigrationInputError, AwsCliError, MigrationTaskFailed) as exc:
        _write_summary(config, result="cleanup-failed", reason=str(exc))
        print(f"::error::{_sanitize(str(exc))}")
        return CONTRACT_FAILURE
    except Exception as exc:
        reason = f"unexpected cleanup failure: {type(exc).__name__}"
        _write_summary(config, result="cleanup-failed", reason=reason)
        print(f"::error::{reason}")
        return CONTRACT_FAILURE


def _validate_config(config: MigrationConfig) -> None:
    if not _SHA_RE.fullmatch(config.release_sha):
        raise MigrationInputError("release SHA must be 40 lowercase hex characters")
    for label, value in (
        ("cluster", config.cluster),
        ("family", config.family),
        ("container", config.container_name),
        ("network", config.network_name),
    ):
        if not _NAME_RE.fullmatch(value):
            raise MigrationInputError(f"invalid {label} name")
    if not _STARTED_BY_RE.fullmatch(config.started_by):
        raise MigrationInputError("invalid startedBy value")
    if config.poll_seconds <= 0 or config.timeout_seconds < 0:
        raise MigrationInputError("poll/timeout values are invalid")


def _registration_for_release(
    config: MigrationConfig,
    response: Mapping[str, object],
) -> tuple[dict[str, object], str, str]:
    raw = response.get("taskDefinition")
    if not isinstance(raw, Mapping):
        raise MigrationInputError("describe-task-definition omitted taskDefinition")
    definition = dict(raw)
    issues = _base_definition_issues(config, definition)
    if issues:
        raise MigrationInputError(", ".join(issues))
    containers = _mapping_items(definition.get("containerDefinitions"))
    container = dict(containers[0])
    repository, _, _ = str(container["image"]).rpartition(":")
    container["image"] = f"{repository}:{config.release_sha}"
    definition["containerDefinitions"] = [container]
    for field in _READ_ONLY_TASK_DEFINITION_FIELDS:
        definition.pop(field, None)
    options = _mapping_value(container.get("logConfiguration"), "log options").get(
        "options"
    )
    log_options = _mapping_value(options, "log options")
    return (
        definition,
        str(log_options.get("awslogs-group", "")),
        str(log_options.get("awslogs-stream-prefix", "")),
    )


def _base_definition_issues(
    config: MigrationConfig,
    definition: Mapping[str, object],
) -> list[str]:
    issues: list[str] = []
    expected = {
        "family": config.family,
        "networkMode": "awsvpc",
        "cpu": "256",
        "memory": "512",
    }
    for key, value in expected.items():
        if str(definition.get(key, "")) != value:
            issues.append(f"base_{key}_mismatch")
    if definition.get("requiresCompatibilities") != ["FARGATE"]:
        issues.append("base_requires_compatibilities_mismatch")
    runtime = _mapping_value(definition.get("runtimePlatform"), "runtimePlatform")
    if runtime.get("cpuArchitecture") != "ARM64":
        issues.append("base_cpu_architecture_mismatch")
    if runtime.get("operatingSystemFamily") != "LINUX":
        issues.append("base_operating_system_mismatch")
    if _resource_suffix(str(definition.get("taskRoleArn", ""))) != (
        "vector-migration-task"
    ):
        issues.append("base_task_role_mismatch")
    if _resource_suffix(str(definition.get("executionRoleArn", ""))) != (
        "vector-migration-exec"
    ):
        issues.append("base_execution_role_mismatch")
    containers = _mapping_items(definition.get("containerDefinitions"))
    if len(containers) != 1 or containers[0].get("name") != config.container_name:
        return [*issues, "base_container_mismatch"]
    container = containers[0]
    if (
        container.get("essential") is not True
        or container.get("privileged") is not False
    ):
        issues.append("base_container_hardening_mismatch")
    if container.get("command") != [
        "python",
        "-m",
        "scripts.run_production_migration",
    ]:
        issues.append("base_command_mismatch")
    if _list_value(container.get("secrets")):
        issues.append("base_secrets_must_be_empty")
    image = str(container.get("image", ""))
    repository, separator, _ = image.rpartition(":")
    if separator == "" or not repository.endswith("/vector/backend"):
        issues.append("base_image_repository_mismatch")
    issues.extend(_environment_issues(container))
    log_configuration = _mapping_value(
        container.get("logConfiguration"), "logConfiguration"
    )
    if log_configuration.get("logDriver") != "awslogs":
        issues.append("base_log_driver_mismatch")
    log_options = _mapping_value(log_configuration.get("options"), "log options")
    if log_options.get("awslogs-group") != "/ecs/vector/migration":
        issues.append("base_log_group_mismatch")
    if log_options.get("awslogs-stream-prefix") != "ecs":
        issues.append("base_log_stream_prefix_mismatch")
    environment = {
        str(item.get("name", "")): str(item.get("value", ""))
        for item in _mapping_items(container.get("environment"))
    }
    if log_options.get("awslogs-region") != environment.get("AWS_REGION"):
        issues.append("base_log_region_mismatch")
    return issues


def _environment_issues(container: Mapping[str, object]) -> list[str]:
    environment = {
        str(item.get("name", "")): str(item.get("value", ""))
        for item in _mapping_items(container.get("environment"))
    }
    if set(environment) != {
        "ENV",
        "AWS_REGION",
        "DB_IAM_AUTH",
        "MIGRATION_DATABASE_URL",
    }:
        return ["base_environment_names_mismatch"]
    issues: list[str] = []
    if environment["ENV"] != "production" or environment["DB_IAM_AUTH"] != "true":
        issues.append("base_environment_mode_mismatch")
    if not environment["AWS_REGION"]:
        issues.append("base_environment_region_missing")
    try:
        url = urlsplit(environment["MIGRATION_DATABASE_URL"])
        query = parse_qs(url.query, keep_blank_values=True)
        if url.username != "vector" or url.password is not None:
            issues.append("base_database_identity_mismatch")
        if set(query) != {"sslmode"}:
            issues.append("base_database_query_mismatch")
        if url.port != 5432 or query.get("sslmode") not in (
            ["require"],
            ["verify-ca"],
            ["verify-full"],
        ):
            issues.append("base_database_transport_mismatch")
    except ValueError:
        issues.append("base_database_url_invalid")
    return issues


def _registered_task_definition_arn(
    response: Mapping[str, object],
    config: MigrationConfig,
) -> str:
    definition = _mapping_value(response.get("taskDefinition"), "registered task")
    arn = str(definition.get("taskDefinitionArn", ""))
    if _task_definition_family(arn) != config.family:
        raise MigrationInputError("registered task definition family mismatch")
    return arn


def _started_task_arn(response: Mapping[str, object]) -> str:
    if _mapping_items(response.get("failures")):
        raise MigrationTaskFailed("RunTask returned placement failures")
    tasks = _mapping_items(response.get("tasks"))
    if len(tasks) != 1 or not tasks[0].get("taskArn"):
        raise MigrationInputError("RunTask must return exactly one task ARN")
    return str(tasks[0]["taskArn"])


def _wait_for_stopped(
    config: MigrationConfig,
    client: MigrationClient,
    task_arn: str,
    *,
    timeout_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> Mapping[str, object]:
    deadline = monotonic() + timeout_seconds
    while True:
        task = _describe_one_task(client, config.cluster, task_arn)
        status = str(task.get("lastStatus", ""))
        if status == "STOPPED":
            return task
        if status not in {
            "PROVISIONING",
            "PENDING",
            "ACTIVATING",
            "RUNNING",
            "DEACTIVATING",
            "STOPPING",
            "DEPROVISIONING",
        }:
            raise MigrationInputError(f"unknown ECS task status: {status or 'missing'}")
        if monotonic() >= deadline:
            raise MigrationTaskTimedOut("migration task timed out")
        sleep(config.poll_seconds)


def _stop_and_wait(
    config: MigrationConfig,
    client: MigrationClient,
    task_arn: str,
    *,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> Mapping[str, object]:
    current = _describe_one_task(client, config.cluster, task_arn)
    _validate_cleanup_target(config, task_arn, current)
    if str(current.get("lastStatus", "")) != "STOPPED":
        client.stop_task(
            config.cluster,
            task_arn,
            "GitHub Actions migration cleanup",
        )
    return _wait_for_stopped(
        config,
        client,
        task_arn,
        timeout_seconds=config.cleanup_timeout_seconds,
        monotonic=monotonic,
        sleep=sleep,
    )


def _describe_one_task(
    client: MigrationClient,
    cluster: str,
    task_arn: str,
) -> Mapping[str, object]:
    response = client.describe_tasks(cluster, [task_arn])
    if _mapping_items(response.get("failures")):
        raise AwsCliError("DescribeTasks returned failures")
    tasks = _mapping_items(response.get("tasks"))
    if len(tasks) != 1 or str(tasks[0].get("taskArn", "")) != task_arn:
        raise MigrationInputError("DescribeTasks did not return the exact task")
    return tasks[0]


def _runtime_issues(
    config: MigrationConfig,
    expected_task_definition: str,
    task: Mapping[str, object],
) -> list[str]:
    issues: list[str] = []
    if str(task.get("taskDefinitionArn", "")) != expected_task_definition:
        issues.append("task_definition_mismatch")
    if str(task.get("lastStatus", "")) != "STOPPED":
        issues.append("task_not_stopped")
    containers = [
        item
        for item in _mapping_items(task.get("containers"))
        if item.get("name") == config.container_name
    ]
    if len(containers) != 1:
        return [*issues, "migration_container_mismatch"]
    container = containers[0]
    if container.get("exitCode") != 0:
        issues.append(f"container_exit_code={container.get('exitCode', 'missing')}")
    if _image_tag(str(container.get("image", ""))) != config.release_sha:
        issues.append("container_image_tag_mismatch")
    return issues


def _validate_cleanup_target(
    config: MigrationConfig,
    task_arn: str,
    task: Mapping[str, object],
) -> None:
    if str(task.get("taskArn", "")) != task_arn:
        raise MigrationInputError("cleanup task ARN mismatch")
    if str(task.get("startedBy", "")) != config.started_by:
        raise MigrationInputError("cleanup task startedBy mismatch")
    if _task_definition_family(str(task.get("taskDefinitionArn", ""))) != config.family:
        raise MigrationInputError("cleanup task family mismatch")
    tags = {
        str(item.get("key", "")): str(item.get("value", ""))
        for item in _mapping_items(task.get("tags"))
    }
    if tags.get("VectorPurpose") != "migration":
        raise MigrationInputError("cleanup task purpose tag mismatch")
    if tags.get("ReleaseSha") != config.release_sha:
        raise MigrationInputError("cleanup task release tag mismatch")
    if tags.get("GitHubRunId") != config.github_run_id:
        raise MigrationInputError("cleanup task run tag mismatch")


def _task_tags(config: MigrationConfig) -> dict[str, str]:
    return {
        "VectorPurpose": "migration",
        "ReleaseSha": config.release_sha,
        "GitHubRunId": config.github_run_id,
    }


def _client_token(config: MigrationConfig) -> str:
    digest = config.release_sha[:16]
    return f"migration-{config.github_run_id}-{digest}"[:64]


def _write_state(path: Path, state: MigrationState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(state.as_dict()) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_state(path: Path) -> MigrationState | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationInputError("migration state file is invalid") from exc
    if not isinstance(payload, dict):
        raise MigrationInputError("migration state file must be an object")
    values = [
        payload.get(key) for key in ("task_arn", "task_definition_arn", "started_by")
    ]
    if not all(isinstance(value, str) and value for value in values):
        raise MigrationInputError("migration state file fields are invalid")
    return MigrationState(*values)


def _write_summary(
    config: MigrationConfig,
    *,
    result: str,
    reason: str = "",
    task: Mapping[str, object] | None = None,
    task_definition_arn: str = "",
    log_group: str = "",
    log_prefix: str = "ecs",
) -> None:
    task = task or {}
    container = _container(task, config.container_name)
    task_id = _resource_suffix(str(task.get("taskArn", ""))) or "-"
    definition = task_definition_arn or str(task.get("taskDefinitionArn", ""))
    image_tag = _image_tag(str(container.get("image", ""))) or "-"
    stream = "-"
    if task_id != "-" and log_group:
        stream = f"{log_prefix}/{config.container_name}/{task_id}"
    lines = [
        "## ECS production migration",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| result | {_markdown(result)} |",
        f"| task | {_markdown(task_id)} |",
        f"| task definition | {_markdown(_resource_suffix(definition) or '-')} |",
        f"| image tag | {_markdown(image_tag)} |",
        f"| exit code | {_markdown(str(container.get('exitCode', '-')))} |",
        f"| stop code | {_markdown(str(task.get('stopCode', '-')))} |",
        f"| stop reason | {_markdown(_safe_stop_reason(task.get('stoppedReason')))} |",
        f"| log group | {_markdown(log_group or '-')} |",
        f"| log stream | {_markdown(stream)} |",
    ]
    if reason:
        lines.append(f"| controller reason | {_markdown(reason)} |")
    config.summary_file.parent.mkdir(parents=True, exist_ok=True)
    with config.summary_file.open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def _one(values: Sequence[str], label: str) -> str:
    if len(values) != 1 or not values[0]:
        raise MigrationInputError(f"expected exactly one {label}, got {len(values)}")
    return str(values[0])


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _mapping_value(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MigrationInputError(f"{label} must be an object")
    return value


def _list_value(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _string_list(
    payload: Mapping[str, object],
    key: str,
    label: str,
) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise AwsCliError(f"{label} omitted {key}")
    return [str(item) for item in value]


def _resource_suffix(value: str) -> str:
    return value.rsplit("/", 1)[-1] if value else ""


def _task_definition_family(arn: str) -> str:
    return _resource_suffix(arn).partition(":")[0]


def _image_tag(image: str) -> str:
    repository, separator, tag = image.rpartition(":")
    if separator == "" or "/" not in repository:
        return ""
    return tag


def _container(task: Mapping[str, object], name: str) -> Mapping[str, object]:
    matches = [
        item
        for item in _mapping_items(task.get("containers"))
        if item.get("name") == name
    ]
    return matches[0] if len(matches) == 1 else {}


def _safe_aws_operation(arguments: Sequence[str]) -> str:
    if len(arguments) < 2:
        return "unknown-operation"
    service, operation = arguments[:2]
    if not re.fullmatch(r"[a-z0-9-]{1,64}", service):
        return "unknown-operation"
    if not re.fullmatch(r"[a-z0-9-]{1,64}", operation):
        return "unknown-operation"
    return f"{service}:{operation}"


def _safe_stop_reason(value: object) -> str:
    reason = str(value or "")
    if not reason:
        return "-"
    for prefix in _SAFE_STOP_REASON_PREFIXES:
        if reason.startswith(prefix):
            return prefix
    return "details withheld; inspect the referenced CloudWatch Logs stream"


def _sanitize(value: object) -> str:
    rendered = str(value)
    rendered = _ARN_RE.sub(
        lambda match: f"[arn:{_resource_suffix(match.group(0))}]", rendered
    )
    rendered = _ACCOUNT_ID_RE.sub("[account]", rendered)

    def redact_ipv4(match: re.Match[str]) -> str:
        candidate = match.group(0)
        try:
            return "[ip]" if ipaddress.ip_address(candidate).is_private else candidate
        except ValueError:
            return candidate

    return _IPV4_RE.sub(redact_ipv4, rendered)


def _markdown(value: object) -> str:
    rendered = _sanitize(value).replace("\r", " ").replace("\n", " ")
    markdown_punctuation = frozenset("\\`*_{}[]()#+-.!|")
    return "".join(
        f"&#{ord(character)};"
        if character in markdown_punctuation
        else html.escape(character, quote=True)
        for character in rendered
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--family", default="vector-migration")
    parser.add_argument("--container-name", default="migration")
    parser.add_argument("--network-name", default="vector-migration")
    parser.add_argument("--started-by", required=True)
    parser.add_argument("--github-run-id", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--timeout-seconds", type=float, default=1200)
    parser.add_argument("--cleanup-timeout-seconds", type=float, default=180)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    aws_path = shutil.which("aws")
    if aws_path is None:
        print("::error::AWS CLI executable was not found")
        return CONTRACT_FAILURE
    config = MigrationConfig(
        cluster=args.cluster,
        release_sha=args.release_sha,
        family=args.family,
        container_name=args.container_name,
        network_name=args.network_name,
        started_by=args.started_by,
        github_run_id=args.github_run_id,
        state_file=args.state_file,
        summary_file=args.summary_file,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        cleanup_timeout_seconds=args.cleanup_timeout_seconds,
    )
    client = AwsCliMigrationClient(aws_path)
    if args.cleanup:
        return cleanup_migration(config, client)
    return run_migration(config, client)


if __name__ == "__main__":
    raise SystemExit(main())
