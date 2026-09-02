"""ECS serviceが期待するTask Definitionと実稼働imageへ収束したか検証する。"""

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

SUCCESS = 0
ROLLOUT_FAILURE = 1
CONTRACT_FAILURE = 2

_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_IMAGE_TAG_RE = re.compile(r"^[0-9a-f]{40}$")
_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:.])"
    r"(?:[0-9A-Fa-f]{0,4}:){2,}"
    r"(?:[0-9A-Fa-f]{0,4}|(?:\d{1,3}\.){3}\d{1,3})"
    r"(?![0-9A-Fa-f:.])"
)
_ARN_RE = re.compile(r"arn:[a-z0-9-]+:[^\s|,;)]+")


class RolloutInputError(ValueError):
    """検証を始められない入力契約違反。"""


class AwsCliError(RuntimeError):
    """AWS CLI呼び出し自体の失敗。"""


class EcsClient(Protocol):
    """rollout検証が使うECS読み取り境界。"""

    def describe_services(
        self, cluster: str, services: Sequence[str]
    ) -> Mapping[str, object]: ...

    def list_tasks(
        self, cluster: str, service: str, desired_status: str
    ) -> Sequence[str]: ...

    def describe_tasks(
        self, cluster: str, task_arns: Sequence[str]
    ) -> Mapping[str, object]: ...

    def describe_task_definition(
        self, task_definition: str
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    """1回のrollout検証条件。"""

    cluster: str
    image_tag: str
    expected_task_definitions: Mapping[str, str]
    rollout_started_at: datetime
    timeout_seconds: float
    poll_seconds: float
    summary_file: Path


@dataclass(slots=True)
class ServiceObservation:
    """1 serviceのcontrol planeとruntimeの観測結果。"""

    name: str
    expected_task_definition: str
    current_task_definition: str = ""
    primary_task_definition: str = ""
    rollout_state: str = "MISSING"
    rollout_reason: str = ""
    desired_count: int = 0
    running_count: int = 0
    pending_count: int = 0
    failed_tasks: int = 0
    old_deployments: list[Mapping[str, object]] = field(default_factory=list)
    events: list[Mapping[str, object]] = field(default_factory=list)
    running_tasks: list[Mapping[str, object]] = field(default_factory=list)
    stopped_tasks: list[Mapping[str, object]] = field(default_factory=list)
    log_group: str = ""
    log_stream_prefix: str = ""
    problems: list[str] = field(default_factory=list)
    contract_errors: list[str] = field(default_factory=list)


class AwsCliEcsClient:
    """AWS CLIのJSON出力だけを読むECS client。"""

    def __init__(self, aws_path: str) -> None:
        self._aws_path = aws_path

    def _run(self, *arguments: str) -> Mapping[str, object]:
        # shellを使わず、引数も固定commandと内部生成値だけを渡す。
        completed = subprocess.run(  # noqa: S603
            [self._aws_path, *arguments, "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "AWS CLI returned no error detail"
            raise AwsCliError(_sanitize(detail))
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AwsCliError("AWS CLI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AwsCliError("AWS CLI response must be a JSON object")
        return payload

    def describe_services(
        self, cluster: str, services: Sequence[str]
    ) -> Mapping[str, object]:
        combined_services: list[object] = []
        combined_failures: list[object] = []
        for chunk in _chunks(services, 10):
            payload = self._run(
                "ecs",
                "describe-services",
                "--cluster",
                cluster,
                "--services",
                *chunk,
            )
            combined_services.extend(
                _required_list(payload, "services", "describe-services")
            )
            combined_failures.extend(_list_value(payload.get("failures")))
        return {"services": combined_services, "failures": combined_failures}

    def list_tasks(
        self, cluster: str, service: str, desired_status: str
    ) -> Sequence[str]:
        payload = self._run(
            "ecs",
            "list-tasks",
            "--cluster",
            cluster,
            "--service-name",
            service,
            "--desired-status",
            desired_status,
        )
        return [
            str(value) for value in _required_list(payload, "taskArns", "list-tasks")
        ]

    def describe_tasks(
        self, cluster: str, task_arns: Sequence[str]
    ) -> Mapping[str, object]:
        if not task_arns:
            return {"tasks": [], "failures": []}
        combined_tasks: list[object] = []
        combined_failures: list[object] = []
        for chunk in _chunks(task_arns, 100):
            payload = self._run(
                "ecs",
                "describe-tasks",
                "--cluster",
                cluster,
                "--tasks",
                *chunk,
            )
            combined_tasks.extend(_required_list(payload, "tasks", "describe-tasks"))
            combined_failures.extend(_list_value(payload.get("failures")))
        return {"tasks": combined_tasks, "failures": combined_failures}

    def describe_task_definition(self, task_definition: str) -> Mapping[str, object]:
        return self._run(
            "ecs",
            "describe-task-definition",
            "--task-definition",
            task_definition,
        )


def verify_rollout(
    config: VerificationConfig,
    client: EcsClient,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """完了までpollし、成功または診断付き失敗のexit codeを返す。"""

    deadline = monotonic() + config.timeout_seconds
    observations: list[ServiceObservation] = []
    failure_title = ""
    exit_code = SUCCESS

    while True:
        try:
            observations = _observe_services(config, client)
        except AwsCliError as exc:
            _write_global_failure(config.summary_file, "AWS API error", str(exc))
            return CONTRACT_FAILURE

        if any(item.rollout_state == "FAILED" for item in observations):
            failure_title = "PRIMARY rollout failed"
            exit_code = ROLLOUT_FAILURE
            break

        if any(item.contract_errors for item in observations):
            failure_title = "ECS response contract violation"
            exit_code = CONTRACT_FAILURE
            break

        states = {item.rollout_state for item in observations}
        if states == {"COMPLETED"}:
            _observe_running_tasks(config, client, observations, validate=True)
            if any(item.contract_errors for item in observations):
                failure_title = "ECS runtime observation failed"
                exit_code = CONTRACT_FAILURE
                break
            if any(item.problems for item in observations):
                failure_title = "Completed rollout violated postconditions"
                exit_code = ROLLOUT_FAILURE
                break
            _write_summary(
                config.summary_file,
                title="ECS rollout verification",
                result="success",
                observations=observations,
            )
            print("ECS rollout verification completed")
            return SUCCESS

        if monotonic() >= deadline:
            failure_title = "ECS rollout verification timed out"
            exit_code = ROLLOUT_FAILURE
            for item in observations:
                if item.rollout_state == "IN_PROGRESS":
                    item.problems.append("rollout_timeout")
            break

        waiting = ", ".join(
            item.name for item in observations if item.rollout_state == "IN_PROGRESS"
        )
        print(f"Waiting for ECS rollout: {waiting}")
        sleep(config.poll_seconds)

    _observe_running_tasks(
        config,
        client,
        observations,
        validate=False,
    )
    _collect_failure_diagnostics(config, client, observations)

    _write_summary(
        config.summary_file,
        title=failure_title,
        result="failure",
        observations=observations,
        include_details=True,
    )
    print(f"::error::{failure_title}")
    return exit_code


def completed_service_issues(
    cluster: str,
    image_tag: str,
    service_names: Sequence[str],
    client: EcsClient,
) -> tuple[str, ...]:
    """待機せず、rolloutと同じ完了条件にtask definitionのimage確認を加える。"""
    if not service_names or len(set(service_names)) != len(service_names):
        return ("service_set_invalid",)
    if not _IMAGE_TAG_RE.fullmatch(image_tag):
        return ("image_tag_invalid",)
    response = client.describe_services(cluster, service_names)
    services = _mapping_items(response.get("services"))
    if response.get("failures") or {
        item.get("serviceName") for item in services
    } != set(service_names):
        return ("service_response_incomplete",)
    expected: dict[str, str] = {}
    for service in services:
        name = str(service["serviceName"])
        definition_arn = service.get("taskDefinition")
        if not isinstance(definition_arn, str) or not definition_arn:
            return ("service_definition_missing",)
        if any(
            type(service.get(field)) is not int or service[field] < 0
            for field in ("desiredCount", "runningCount", "pendingCount")
        ):
            return ("service_counts_invalid",)
        expected[name] = definition_arn
    config = VerificationConfig(
        cluster=cluster,
        image_tag=image_tag,
        expected_task_definitions=expected,
        rollout_started_at=datetime.now(UTC),
        timeout_seconds=0,
        poll_seconds=1,
        summary_file=Path("unused"),
    )
    observations = _observe_services(config, client)
    if any(
        item.rollout_state != "COMPLETED" or item.problems or item.contract_errors
        for item in observations
    ):
        return ("service_rollout_incomplete",)
    _observe_running_tasks(config, client, observations, validate=True)
    if any(item.problems or item.contract_errors for item in observations):
        return ("service_runtime_mismatch",)
    for name, definition_arn in expected.items():
        response = client.describe_task_definition(definition_arn)
        definition = response.get("taskDefinition")
        if not isinstance(definition, Mapping):
            return ("service_definition_missing",)
        containers = [
            item
            for item in _mapping_items(definition.get("containerDefinitions"))
            if item.get("name") == name
        ]
        if (
            definition.get("taskDefinitionArn") != definition_arn
            or len(containers) != 1
            or _image_tag(str(containers[0].get("image", ""))) != image_tag
        ):
            return ("service_definition_image_mismatch",)
    return ()


def _observe_services(
    config: VerificationConfig, client: EcsClient
) -> list[ServiceObservation]:
    names = sorted(config.expected_task_definitions)
    payload = client.describe_services(config.cluster, names)
    raw_services = {
        str(item.get("serviceName")): item
        for item in _mapping_items(payload.get("services"))
    }
    failures = {
        _resource_suffix(str(item.get("arn", ""))): item
        for item in _mapping_items(payload.get("failures"))
    }
    observations: list[ServiceObservation] = []

    for name in names:
        observation = ServiceObservation(
            name=name,
            expected_task_definition=config.expected_task_definitions[name],
        )
        raw = raw_services.get(name)
        if raw is None:
            failure = failures.get(name, {})
            reason = str(failure.get("reason", "service_missing"))
            observation.contract_errors.append(f"service_missing: {reason}")
            observations.append(observation)
            continue

        observation.current_task_definition = str(raw.get("taskDefinition", ""))
        observation.desired_count = _int_value(raw.get("desiredCount"))
        observation.running_count = _int_value(raw.get("runningCount"))
        observation.pending_count = _int_value(raw.get("pendingCount"))
        observation.events = _mapping_items(raw.get("events"))[:5]
        deployments = _mapping_items(raw.get("deployments"))
        primary = [item for item in deployments if item.get("status") == "PRIMARY"]
        if len(primary) != 1:
            observation.contract_errors.append(
                f"primary_deployment_count={len(primary)}"
            )
            observations.append(observation)
            continue

        primary_deployment = primary[0]
        observation.primary_task_definition = str(
            primary_deployment.get("taskDefinition", "")
        )
        observation.rollout_state = str(
            primary_deployment.get("rolloutState", "MISSING")
        )
        observation.rollout_reason = str(
            primary_deployment.get("rolloutStateReason", "")
        )
        observation.failed_tasks = _int_value(primary_deployment.get("failedTasks"))
        observation.old_deployments = [
            item for item in deployments if item is not primary_deployment
        ]
        if observation.rollout_state not in {"IN_PROGRESS", "COMPLETED", "FAILED"}:
            observation.contract_errors.append(
                f"unknown_rollout_state={observation.rollout_state}"
            )
        if observation.rollout_state == "FAILED":
            observation.problems.append("primary_rollout_failed")
        if observation.rollout_state == "COMPLETED":
            _validate_service_postconditions(observation)
        observations.append(observation)

    return observations


def _validate_service_postconditions(observation: ServiceObservation) -> None:
    if observation.current_task_definition != observation.expected_task_definition:
        observation.problems.append("service_task_definition_mismatch")
    if observation.primary_task_definition != observation.expected_task_definition:
        observation.problems.append("primary_task_definition_mismatch")
    if observation.running_count != observation.desired_count:
        observation.problems.append("running_count_mismatch")
    if observation.pending_count != 0:
        observation.problems.append("pending_tasks_remaining")
    if any(
        _int_value(item.get("runningCount")) != 0
        or _int_value(item.get("pendingCount")) != 0
        for item in observation.old_deployments
    ):
        observation.problems.append("old_deployment_active")


def _observe_running_tasks(
    config: VerificationConfig,
    client: EcsClient,
    observations: Sequence[ServiceObservation],
    *,
    validate: bool,
) -> None:
    for observation in observations:
        try:
            task_arns = client.list_tasks(config.cluster, observation.name, "RUNNING")
            payload = client.describe_tasks(config.cluster, task_arns)
        except AwsCliError as exc:
            error = f"running_task_observation_failed: {_sanitize(str(exc))}"
            target = observation.contract_errors if validate else observation.problems
            target.append(error)
            continue
        failures = _mapping_items(payload.get("failures"))
        if failures:
            target = observation.contract_errors if validate else observation.problems
            target.append("running_task_describe_failure")
        observation.running_tasks = _mapping_items(payload.get("tasks"))
        if not validate or observation.rollout_state != "COMPLETED":
            continue
        if len(observation.running_tasks) != observation.desired_count:
            observation.problems.append("running_task_count_mismatch")
        for task in observation.running_tasks:
            if (
                str(task.get("taskDefinitionArn", ""))
                != observation.expected_task_definition
            ):
                observation.problems.append("running_task_definition_mismatch")
            if str(task.get("lastStatus", "")) != "RUNNING":
                observation.problems.append("running_task_status_mismatch")
            containers = [
                item
                for item in _mapping_items(task.get("containers"))
                if item.get("name") == observation.name
            ]
            if len(containers) != 1:
                observation.problems.append("service_container_count_mismatch")
                continue
            container = containers[0]
            if str(container.get("lastStatus", "")) != "RUNNING":
                observation.problems.append("service_container_not_running")
            if _image_tag(str(container.get("image", ""))) != config.image_tag:
                observation.problems.append("running_container_image_tag_mismatch")
        observation.problems = list(dict.fromkeys(observation.problems))


def _collect_failure_diagnostics(
    config: VerificationConfig,
    client: EcsClient,
    observations: Sequence[ServiceObservation],
) -> None:
    for observation in observations:
        if observation.rollout_state == "COMPLETED" and not (
            observation.problems or observation.contract_errors
        ):
            continue
        try:
            task_arns = client.list_tasks(config.cluster, observation.name, "STOPPED")
            payload = client.describe_tasks(config.cluster, task_arns)
            if _mapping_items(payload.get("failures")):
                observation.problems.append("stopped_task_describe_failure")
            stopped_tasks = [
                item
                for item in _mapping_items(payload.get("tasks"))
                if str(item.get("taskDefinitionArn", ""))
                == observation.expected_task_definition
                and _task_time(item) >= config.rollout_started_at
            ]
            observation.stopped_tasks = sorted(
                stopped_tasks,
                key=_task_time,
                reverse=True,
            )[:3]
            task_definition = client.describe_task_definition(
                observation.expected_task_definition
            )
            definition = task_definition.get("taskDefinition")
            if not isinstance(definition, dict):
                observation.problems.append("task_definition_diagnostics_missing")
                continue
            containers = [
                item
                for item in _mapping_items(definition.get("containerDefinitions"))
                if item.get("name") == observation.name
            ]
            if len(containers) != 1:
                observation.problems.append("log_container_diagnostics_missing")
                continue
            log_configuration = containers[0].get("logConfiguration")
            if not isinstance(log_configuration, dict):
                observation.problems.append("log_configuration_diagnostics_missing")
                continue
            options = log_configuration.get("options")
            if not isinstance(options, dict):
                observation.problems.append("log_options_diagnostics_missing")
                continue
            observation.log_group = str(options.get("awslogs-group", ""))
            observation.log_stream_prefix = str(
                options.get("awslogs-stream-prefix", "")
            )
        except AwsCliError as exc:
            observation.problems.append(
                f"diagnostic_collection_failed: {_sanitize(str(exc))}"
            )
        observation.problems = list(dict.fromkeys(observation.problems))


def _write_summary(
    path: Path,
    *,
    title: str,
    result: str,
    observations: Sequence[ServiceObservation],
    include_details: bool = False,
) -> None:
    lines = [
        f"## {_markdown(title)}",
        "",
        f"Result: **{_markdown(result)}**",
        "",
        (
            "| service | result | PRIMARY | reason | expected TD | current TD | "
            "desired/running/pending | failedTasks | running tasks | image tag |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for observation in observations:
        image_tags = _running_image_tags(observation)
        service_result = _service_result(observation)
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown(observation.name),
                    _markdown(service_result),
                    _markdown(observation.rollout_state),
                    _markdown(observation.rollout_reason or "-"),
                    _markdown(_resource_suffix(observation.expected_task_definition)),
                    _markdown(_resource_suffix(observation.current_task_definition)),
                    _markdown(
                        f"{observation.desired_count}/{observation.running_count}/{observation.pending_count}"
                    ),
                    str(observation.failed_tasks),
                    str(len(observation.running_tasks)),
                    _markdown(", ".join(image_tags) or "-"),
                ]
            )
            + " |"
        )

    if include_details:
        for observation in observations:
            if _service_result(observation) == "success":
                continue
            lines.extend(_diagnostic_details(observation))

    with path.open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def _diagnostic_details(observation: ServiceObservation) -> list[str]:
    issues = observation.contract_errors + observation.problems
    lines = [
        "",
        f"<details><summary>{_markdown(observation.name)} diagnostics</summary>",
        "",
        f"- issues: {_markdown(', '.join(issues) or 'rollout_incomplete')}",
        f"- rollout reason: {_markdown(observation.rollout_reason or '-')}",
    ]
    if observation.log_group:
        lines.append(f"- log group: `{_markdown(observation.log_group)}`")
    if observation.running_tasks:
        lines.extend(
            [
                "- running tasks:",
                "",
                (
                    "  | task | TD | task status | container status | image tag | "
                    "log stream |"
                ),
                "  | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for task in observation.running_tasks:
            container = _service_container(task, observation.name)
            lines.append(
                "  | "
                + " | ".join(
                    [
                        _markdown(_resource_suffix(str(task.get("taskArn", "")))),
                        _markdown(
                            _resource_suffix(str(task.get("taskDefinitionArn", "")))
                        ),
                        _markdown(str(task.get("lastStatus", "-"))),
                        _markdown(str(container.get("lastStatus", "-"))),
                        _markdown(_image_tag(str(container.get("image", ""))) or "-"),
                        _markdown(_log_stream(observation, task) or "-"),
                    ]
                )
                + " |"
            )
    if observation.old_deployments:
        lines.extend(
            [
                "- old deployments:",
                "",
                "  | TD | status | rollout | desired/running/pending |",
                "  | --- | --- | --- | --- |",
            ]
        )
        for deployment in observation.old_deployments:
            lines.append(
                "  | "
                + " | ".join(
                    [
                        _markdown(
                            _resource_suffix(str(deployment.get("taskDefinition", "")))
                        ),
                        _markdown(str(deployment.get("status", "-"))),
                        _markdown(str(deployment.get("rolloutState", "-"))),
                        _markdown(
                            "/".join(
                                str(_int_value(deployment.get(key)))
                                for key in (
                                    "desiredCount",
                                    "runningCount",
                                    "pendingCount",
                                )
                            )
                        ),
                    ]
                )
                + " |"
            )
    if observation.stopped_tasks:
        lines.extend(["- stopped tasks since rollout:", ""])
        for task in observation.stopped_tasks:
            container = _service_container(task, observation.name)
            task_id = _markdown(_resource_suffix(str(task.get("taskArn", ""))))
            stop_code = _markdown(str(task.get("stopCode", "-")))
            stopped_reason = _markdown(str(task.get("stoppedReason", "-")))
            exit_code = _markdown(str(container.get("exitCode", "-")))
            container_reason = _markdown(str(container.get("reason", "-")))
            lines.extend(
                [
                    f"  - `{task_id}`",
                    f"    - stop: {stop_code} / {stopped_reason}",
                    f"    - container: exit={exit_code} reason={container_reason}",
                ]
            )
            stream = _log_stream(observation, task)
            if observation.log_group:
                log_group = _markdown(observation.log_group)
                log_stream = _markdown(stream or "-")
                lines.append(f"    - logs: `{log_group}` / `{log_stream}`")
    if observation.events:
        lines.extend(["- recent ECS events:", ""])
        for event in observation.events[:5]:
            created_at = str(event.get("createdAt", "-"))
            message = str(event.get("message", "-"))
            lines.append(f"  - {_markdown(created_at)}: {_markdown(message)}")
    lines.extend(["", "</details>"])
    return lines


def _write_global_failure(path: Path, title: str, detail: str) -> None:
    with path.open("a", encoding="utf-8") as summary:
        summary.write(
            f"## {_markdown(title)}\n\nResult: **failure**\n\n{_markdown(detail)}\n"
        )
    print(f"::error::{_sanitize(title)}")


def _service_result(observation: ServiceObservation) -> str:
    if observation.contract_errors or observation.problems:
        return "failure"
    if observation.rollout_state == "COMPLETED":
        return "success"
    return "in_progress"


def _running_image_tags(observation: ServiceObservation) -> list[str]:
    tags: list[str] = []
    for task in observation.running_tasks:
        container = _service_container(task, observation.name)
        tag = _image_tag(str(container.get("image", "")))
        if tag:
            tags.append(tag)
    return sorted(set(tags))


def _service_container(
    task: Mapping[str, object], service_name: str
) -> Mapping[str, object]:
    containers = [
        item
        for item in _mapping_items(task.get("containers"))
        if item.get("name") == service_name
    ]
    return containers[0] if len(containers) == 1 else {}


def _log_stream(observation: ServiceObservation, task: Mapping[str, object]) -> str:
    if not observation.log_stream_prefix:
        return ""
    task_id = _resource_suffix(str(task.get("taskArn", "")))
    return f"{observation.log_stream_prefix}/{observation.name}/{task_id}"


def _task_time(task: Mapping[str, object]) -> datetime:
    for key in ("stoppedAt", "createdAt"):
        parsed = _parse_datetime(task.get(key))
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=UTC)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _image_tag(image: str) -> str | None:
    final_segment = image.rsplit("/", 1)[-1]
    if "@" in final_segment or ":" not in final_segment:
        return None
    return final_segment.rsplit(":", 1)[1]


def _resource_suffix(value: str) -> str:
    if not value:
        return "-"
    return value.rsplit("/", 1)[-1]


def _sanitize(value: str) -> str:
    value = _ARN_RE.sub("<ARN>", value)
    value = _ACCOUNT_ID_RE.sub("<ACCOUNT_ID>", value)

    def redact_private_ip(match: re.Match[str]) -> str:
        candidate = match.group(0)
        try:
            return (
                "<PRIVATE_IP>"
                if ipaddress.ip_address(candidate).is_private
                else candidate
            )
        except ValueError:
            return candidate

    value = _IPV4_RE.sub(redact_private_ip, value)
    return _IPV6_RE.sub(redact_private_ip, value)


def _markdown(value: str) -> str:
    return (
        html.escape(_sanitize(value), quote=False)
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    return [item for item in _list_value(value) if isinstance(item, dict)]


def _list_value(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _required_list(
    payload: Mapping[str, object], key: str, operation: str
) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise AwsCliError(f"{operation} response must contain a {key} list")
    return value


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _load_expected_task_definitions(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RolloutInputError("expected Task Definition JSONを読めない") from exc
    if not isinstance(payload, dict) or not payload:
        raise RolloutInputError("expected Task Definition JSONは空でないobjectが必要")
    expected: dict[str, str] = {}
    for service, task_definition in payload.items():
        if not isinstance(service, str) or not _SERVICE_NAME_RE.fullmatch(service):
            raise RolloutInputError("service名がECSの形式に一致しない")
        if (
            not isinstance(task_definition, str)
            or ":task-definition/" not in task_definition
        ):
            raise RolloutInputError(f"{service}のTask Definition ARNが不正")
        expected[service] = task_definition
    return expected


def _parse_args(argv: Sequence[str]) -> VerificationConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--expected-task-definitions", type=Path, required=True)
    parser.add_argument("--rollout-started-at", required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    args = parser.parse_args(argv)

    if not _SERVICE_NAME_RE.fullmatch(args.cluster):
        raise RolloutInputError("cluster名がECSの形式に一致しない")
    if not _IMAGE_TAG_RE.fullmatch(args.image_tag):
        raise RolloutInputError("image tagは40桁の小文字16進SHAが必要")
    rollout_started_at = _parse_datetime(args.rollout_started_at)
    if rollout_started_at is None:
        raise RolloutInputError("rollout開始時刻はISO 8601形式が必要")
    if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
        raise RolloutInputError("pollとtimeoutは正数が必要")

    return VerificationConfig(
        cluster=args.cluster,
        image_tag=args.image_tag,
        expected_task_definitions=_load_expected_task_definitions(
            args.expected_task_definitions
        ),
        rollout_started_at=rollout_started_at,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        summary_file=args.summary_file,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""

    try:
        config = _parse_args(argv if argv is not None else sys.argv[1:])
        aws_path = shutil.which("aws")
        if aws_path is None:
            raise RolloutInputError("aws CLIが見つからない")
    except RolloutInputError as exc:
        print(f"::error::{_sanitize(str(exc))}")
        return CONTRACT_FAILURE
    return verify_rollout(config, AwsCliEcsClient(aws_path))


if __name__ == "__main__":
    raise SystemExit(main())
