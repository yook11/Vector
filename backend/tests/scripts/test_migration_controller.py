"""承認後の前提・ledger・所有taskの境界をfake APIで固定する。"""

from __future__ import annotations

import copy
import importlib
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / ".github/scripts"
sys.path.insert(0, str(_SCRIPTS))
controller = importlib.import_module("migration_controller")
prepare = importlib.import_module("migration_prepare")
sys.path.remove(str(_SCRIPTS))

pytestmark = pytest.mark.unit

_SHA = "a" * 40
_TREE = "b" * 40
_PARENT = "c" * 40
_DIGEST = "sha256:" + "d" * 64
_REPOSITORY = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/vector/backend"
_BASE = (
    "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/vector-migration-base:2"
)
_EXECUTION = (
    "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/vector-migration:8"
)
_TASK = "arn:aws:ecs:ap-northeast-1:123456789012:task/vector/migration-task"
_APP_TD = "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/vector-api:3"
_APP_TASK = "arn:aws:ecs:ap-northeast-1:123456789012:task/vector/api-task"


class LedgerApi:
    def __init__(self, events):
        self.events = events
        self.deployments = []
        self.statuses = {}
        self.fail_progress = False
        self.fail_success = False

    def get_json(self, path):
        parts = path.split("?", 1)[0].strip("/").split("/")
        if len(parts) == 1:
            return copy.deepcopy(self.deployments)
        if len(parts) == 2:
            return copy.deepcopy(
                next(d for d in self.deployments if d["id"] == int(parts[1]))
            )
        return copy.deepcopy(self.statuses.get(int(parts[1]), []))

    def post_json(self, path, body):
        if path == "/deployments":
            number = len(self.deployments) + 1
            item = {
                **copy.deepcopy(body),
                "id": number,
                "sha": body["ref"],
                "created_at": f"2026-09-02T00:00:{number:02d}Z",
            }
            self.deployments.append(item)
            self.events.append("ledger_create")
            return copy.deepcopy(item)
        state = body["state"]
        self.events.append("ledger_" + state)
        if (state == "in_progress" and self.fail_progress) or (
            state == "success" and self.fail_success
        ):
            raise RuntimeError("private response must not escape")
        number = int(path.split("/")[2])
        statuses = self.statuses.setdefault(number, [])
        status_id = 100 + len(statuses)
        item = {
            "id": status_id,
            "state": state,
            "created_at": f"2026-09-02T00:01:{len(statuses):02d}Z",
        }
        statuses.append(item)
        return copy.deepcopy(item)


def _base():
    return {
        "taskDefinition": {
            "taskDefinitionArn": _BASE,
            "family": "vector-migration-base",
            "status": "ACTIVE",
            "revision": 2,
            "networkMode": "awsvpc",
            "cpu": "256",
            "memory": "512",
            "requiresCompatibilities": ["FARGATE"],
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
                    "image": f"{_REPOSITORY}:base",
                    "essential": True,
                    "privileged": False,
                    "command": ["python", "-m", "scripts.migration_runner"],
                    "environment": [
                        {"name": "ENV", "value": "production"},
                        {"name": "AWS_REGION", "value": "ap-northeast-1"},
                        {"name": "DB_IAM_AUTH", "value": "true"},
                        {
                            "name": "MIGRATION_DATABASE_URL",
                            "value": "postgresql+asyncpg://vector@db.example:5432/vector?sslmode=require",
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
        },
    }


class EcsApi:
    def __init__(self, events):
        self.events = events
        self.base = _base()
        self.definitions = []
        self.described = []
        self.existing = []
        self.exit_code = 0
        self.digest = _DIGEST
        self.running = False
        self.stop_calls = []
        self.run_unknown = False
        self.stop_unknown = False
        self.other_owner = False
        self.registration_changed = False
        self.registration_duplicate_env = False
        self.service_names = ["proxy", "api"]
        self.app_state = "COMPLETED"
        self.app_tag = _PARENT
        self.app_td_tag = _PARENT
        self.old_running = 0

    def list_active_tasks(self, cluster, family):
        assert (cluster, family) == ("vector", "vector-migration")
        return self.existing

    def find_subnet_ids(self, name):
        assert name == "vector-migration"
        return ["subnet-only-migration"]

    def find_security_group_ids(self, name):
        assert name == "vector-migration"
        return ["sg-only-migration"]

    def describe_task_definition(self, family):
        self.described.append(family)
        if family == _APP_TD:
            return {
                "taskDefinition": {
                    "taskDefinitionArn": _APP_TD,
                    "containerDefinitions": [
                        {"name": "api", "image": f"{_REPOSITORY}:{self.app_td_tag}"},
                    ],
                }
            }
        assert family == "vector-migration-base"
        return copy.deepcopy(self.base)

    def register_task_definition(self, definition, tags):
        self.events.append("register")
        self.definitions.append(copy.deepcopy(definition))
        result = {
            "taskDefinition": {
                **copy.deepcopy(definition),
                "taskDefinitionArn": _EXECUTION,
            }
        }
        if self.registration_changed:
            result["taskDefinition"]["containerDefinitions"][0]["command"] = ["true"]
        container = result["taskDefinition"]["containerDefinitions"][0]
        container["environment"].reverse()
        container.pop("secrets", None)
        if self.registration_duplicate_env:
            container["environment"].append(copy.deepcopy(container["environment"][0]))
        return result

    def run_task(self, **kwargs):
        self.events.append("run")
        assert kwargs["task_definition"] == _EXECUTION
        if self.run_unknown:
            raise RuntimeError("private AWS response")
        return {"tasks": [{"taskArn": _TASK}], "failures": []}

    def _task(self):
        status = "RUNNING" if self.running else "STOPPED"
        return {
            "taskArn": _TASK,
            "taskDefinitionArn": _EXECUTION,
            "lastStatus": status,
            "startedBy": "someone-else"
            if self.other_owner
            else "vector-migration-987-2",
            "stopCode": "EssentialContainerExited",
            "tags": [
                {"key": key, "value": value}
                for key, value in {
                    "VectorPurpose": "migration",
                    "ReleaseSha": _SHA,
                    "GitHubRunId": "987-2",
                }.items()
            ],
            "containers": [
                {
                    "name": "migration",
                    "lastStatus": status,
                    "exitCode": self.exit_code,
                    "image": f"{_REPOSITORY}@{_DIGEST}",
                    "imageDigest": self.digest,
                }
            ],
        }

    def describe_tasks(self, cluster, task_arns):
        if task_arns == [_APP_TASK]:
            return {
                "tasks": [
                    {
                        "taskArn": _APP_TASK,
                        "taskDefinitionArn": _APP_TD,
                        "lastStatus": "RUNNING",
                        "containers": [
                            {
                                "name": "api",
                                "lastStatus": "RUNNING",
                                "image": f"{_REPOSITORY}:{self.app_tag}",
                            }
                        ],
                    }
                ],
                "failures": [],
            }
        assert task_arns == [_TASK]
        return {"tasks": [self._task()], "failures": []}

    def list_started_tasks(self, cluster, family, started_by):
        return [_TASK] if self.run_unknown else []

    def stop_task(self, cluster, task_arn, reason):
        self.stop_calls.append(task_arn)
        if self.stop_unknown:
            raise RuntimeError("private AWS response")
        self.running = False

    def list_services(self, cluster):
        return self.service_names

    def describe_services(self, cluster, names):
        assert names == ["api"]
        return {
            "services": [
                {
                    "serviceName": "api",
                    "taskDefinition": _APP_TD,
                    "desiredCount": 1,
                    "runningCount": 1,
                    "pendingCount": 0,
                    "deployments": [
                        {
                            "status": "PRIMARY",
                            "taskDefinition": _APP_TD,
                            "rolloutState": self.app_state,
                        },
                        {
                            "status": "ACTIVE",
                            "runningCount": self.old_running,
                            "pendingCount": 0,
                        },
                    ],
                }
            ],
            "failures": [],
        }

    def list_tasks(self, cluster, service, desired_status):
        assert (service, desired_status) == ("api", "RUNNING")
        return [_APP_TASK]


def _setup(tmp_path, mode="verify"):
    events = []
    api = LedgerApi(events)
    ledger = controller.MigrationLedger(api)
    if mode != "verify":
        baseline = ledger.read_latest()
        record = controller.LedgerRecord(
            1, _PARENT, "verify", None, "r0", "e" * 40, 1, 1, None, None
        )
        ledger.finish(ledger.begin(baseline, record), "success")
        events.clear()
    prepared = prepare.PreparedMigration(
        release_sha=_SHA,
        mode=mode,
        target_revision="r1",
        migration_tree_oid=_TREE,
        expected_start_revision=None if mode == "verify" else "r0",
        baseline=ledger.read_latest(),
        contract_parent_sha=_PARENT if mode == "contract" else None,
        run_id=987,
        run_attempt=2,
        result="ready",
        pending_revisions=None if mode == "verify" else ("r1",),
        classifications=(),
    )
    evidence = SimpleNamespace(
        release_sha=_SHA,
        migration_tree_oid=_TREE,
        protocol_version=1,
        image_digest=_DIGEST,
        run_id=987,
        run_attempt=2,
    )
    config = controller.MigrationConfig(
        cluster="vector",
        release_sha=_SHA,
        family="vector-migration",
        container_name="migration",
        network_name="vector-migration",
        started_by="vector-migration-987-2",
        github_run_id="987-2",
        state_file=tmp_path / "state.json",
        summary_file=tmp_path / "summary.md",
        poll_seconds=1,
        timeout_seconds=0,
        cleanup_timeout_seconds=0,
    )
    return config, prepared, evidence, EcsApi(events), ledger, api, events


@pytest.mark.parametrize("mode", ["verify", "expand", "contract"])
def test_ledger_begin_precedes_execution_and_only_verified_exit_records_success(
    tmp_path, mode
):
    config, prepared, evidence, ecs, ledger, api, events = _setup(tmp_path, mode)
    result = controller.run_controller(
        config,
        prepared,
        evidence,
        ecs,
        ledger,
        revalidate=lambda: events.append("revalidate"),
    )
    environment = {
        item["name"]: item["value"]
        for item in ecs.definitions[0]["containerDefinitions"][0]["environment"]
    }

    assert (
        result,
        events,
        ledger.read_latest().state,
        environment["MIGRATION_MODE"],
        ecs.definitions[0]["family"],
        ecs.definitions[0]["containerDefinitions"][0]["image"],
    ) == (
        0,
        [
            "revalidate",
            "ledger_create",
            "ledger_in_progress",
            "register",
            "run",
            "ledger_success",
        ],
        "available",
        mode,
        "vector-migration",
        f"{_REPOSITORY}@{_DIGEST}",
    )


@pytest.mark.parametrize(
    "invalid", ["protocol", "tree", "run", "active", "base", "baseline", "revalidation"]
)
def test_changed_or_invalid_preconditions_never_register_or_start(tmp_path, invalid):
    config, prepared, evidence, ecs, ledger, api, events = _setup(tmp_path)
    if invalid == "protocol":
        evidence.protocol_version = 0
    if invalid == "tree":
        evidence.migration_tree_oid = "e" * 40
    if invalid == "run":
        evidence.run_attempt = 1
    if invalid == "active":
        ecs.existing = ["old-migration"]
    if invalid == "base":
        ecs.base["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "MIGRATION_MODE", "value": "contract"}
        )
    if invalid == "baseline":
        ledger.begin(prepared.baseline, controller._record(prepared))
        events.clear()

    def revalidate():
        if invalid == "revalidation":
            raise ValueError("private response")

    result = controller.run_controller(
        config, prepared, evidence, ecs, ledger, revalidate=revalidate
    )

    assert (result, events) == (2, [])


@pytest.mark.parametrize(
    "invalid", ["empty", "in_progress", "old", "runtime", "definition"]
)
def test_contract_requires_completed_parent_application_without_updates(
    tmp_path, invalid
):
    config, prepared, evidence, ecs, ledger, api, events = _setup(tmp_path, "contract")
    if invalid == "empty":
        ecs.service_names = ["proxy"]
    if invalid == "in_progress":
        ecs.app_state = "IN_PROGRESS"
    if invalid == "old":
        ecs.old_running = 1
    if invalid == "runtime":
        ecs.app_tag = _SHA
    if invalid == "definition":
        ecs.app_td_tag = _SHA
    result = controller.run_controller(
        config, prepared, evidence, ecs, ledger, revalidate=lambda: None
    )

    assert (result, events, ledger.read_latest().record.release_sha) == (2, [], _PARENT)


def test_consecutive_runs_always_use_base_not_previous_runtime_environment(tmp_path):
    config, prepared, evidence, ecs, ledger, api, events = _setup(tmp_path)
    first = controller.run_controller(
        config, prepared, evidence, ecs, ledger, revalidate=lambda: None
    )
    second_prepared = replace(
        prepared, baseline=ledger.read_latest(), expected_start_revision="r1"
    )
    second = controller.run_controller(
        config, second_prepared, evidence, ecs, ledger, revalidate=lambda: None
    )
    environments = [
        {
            item["name"]: item["value"]
            for item in definition["containerDefinitions"][0]["environment"]
        }
        for definition in ecs.definitions
    ]

    assert (
        first,
        second,
        ecs.described,
        environments[0].get("MIGRATION_EXPECTED_START_REVISION"),
        environments[1]["MIGRATION_EXPECTED_START_REVISION"],
        ecs.base,
    ) == (
        0,
        0,
        ["vector-migration-base", "vector-migration-base"],
        None,
        "r1",
        _base(),
    )


@pytest.mark.parametrize(
    "failure",
    [
        "begin",
        "exit3",
        "digest",
        "timeout",
        "unknown_start",
        "unknown_stop",
        "unknown_success",
        "owner",
        "registration",
        "duplicate_environment",
    ],
)
def test_partial_or_failed_execution_never_reports_success_and_only_cleans_owned_task(
    tmp_path, failure, capsys
):
    config, prepared, evidence, ecs, ledger, api, events = _setup(tmp_path)
    if failure == "begin":
        api.fail_progress = True
    if failure == "exit3":
        ecs.exit_code = 3
    if failure == "digest":
        ecs.digest = "sha256:" + "e" * 64
    if failure == "timeout":
        ecs.running = True
    if failure == "unknown_start":
        ecs.run_unknown = ecs.running = True
    if failure == "unknown_stop":
        ecs.running = ecs.stop_unknown = True
    if failure == "unknown_success":
        api.fail_success = True
    if failure == "owner":
        ecs.other_owner = True
    if failure == "registration":
        ecs.registration_changed = True
    if failure == "duplicate_environment":
        ecs.registration_duplicate_env = True
    result = controller.run_controller(
        config, prepared, evidence, ecs, ledger, revalidate=lambda: None
    )
    output = capsys.readouterr()

    assert (
        result != 0,
        ledger.read_latest().state,
        "private" in output.out + output.err + config.summary_file.read_text(),
        ecs.stop_calls
        if failure == "owner"
        else all(arn == _TASK for arn in ecs.stop_calls),
        "run" not in events
        if failure in {"begin", "registration", "duplicate_environment"}
        else True,
    ) == (True, "unavailable", False, [] if failure == "owner" else True, True)


@pytest.mark.parametrize("previous", ["success", "interrupted", "unknown_start"])
def test_cleanup_updates_only_observed_attempt_and_preserves_unknown_outcome(
    tmp_path, previous
):
    config, prepared, evidence, ecs, ledger, api, events = _setup(tmp_path)
    if previous == "interrupted":
        ecs.running = ecs.stop_unknown = True
    if previous == "unknown_start":
        ecs.running = ecs.run_unknown = True
    controller.run_controller(
        config, prepared, evidence, ecs, ledger, revalidate=lambda: None
    )
    events.clear()
    ecs.stop_unknown = False
    result = controller.cleanup_controller(config, prepared, ecs, ledger)

    assert (result, events, ledger.read_latest().latest_status) == {
        "success": (0, [], "success"),
        "interrupted": (0, ["ledger_failure"], "failure"),
        "unknown_start": (1, [], "in_progress"),
    }[previous]
