"""承認後の前提確認とledgerをone-off migrationへ接続し、アプリは更新しない。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

from migration_ecs import (
    AwsCliMigrationClient,
    MigrationConfig,
    MigrationInputError,
    MigrationTaskControl,
    cleanup_migration,
    prepare_task_definition,
)
from migration_ledger import (
    GitHubCli,
    LedgerError,
    LedgerRecord,
    MigrationLedger,
    StartedAttempt,
)
from verify_ecs_rollout import completed_service_issues

if TYPE_CHECKING:
    from migration_image import ImageEvidence
    from migration_prepare import PreparedMigration


class ControllerError(ValueError):
    """接続情報やAPIの生応答を含まないcontroller契約違反。"""


class ControllerInterrupted(Exception):
    pass


def _string_list(payload: Mapping[str, object], key: str) -> list[str]:
    values = payload.get(key)
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ControllerError("aws_list_invalid")
    return values


class AwsMigrationControllerClient(AwsCliMigrationClient):
    def list_services(self, cluster: str) -> Sequence[str]:
        return _string_list(
            self._run("ecs", "list-services", "--cluster", cluster), "serviceArns"
        )

    def describe_services(
        self, cluster: str, services: Sequence[str]
    ) -> Mapping[str, object]:
        result: dict[str, list[object]] = {"services": [], "failures": []}
        for offset in range(0, len(services), 10):
            response = self._run(
                "ecs",
                "describe-services",
                "--cluster",
                cluster,
                "--services",
                *services[offset : offset + 10],
            )
            for field in result:
                values = response.get(field)
                if not isinstance(values, list):
                    raise ControllerError("service_response_invalid")
                result[field].extend(values)
        return result

    def list_tasks(
        self, cluster: str, service: str, desired_status: str
    ) -> Sequence[str]:
        return _string_list(
            self._run(
                "ecs",
                "list-tasks",
                "--cluster",
                cluster,
                "--service-name",
                service,
                "--desired-status",
                desired_status,
            ),
            "taskArns",
        )

    def describe_tasks(
        self, cluster: str, task_arns: Sequence[str]
    ) -> Mapping[str, object]:
        result: dict[str, list[object]] = {"tasks": [], "failures": []}
        for offset in range(0, len(task_arns), 100):
            response = super().describe_tasks(cluster, task_arns[offset : offset + 100])
            for field in result:
                values = response.get(field)
                if not isinstance(values, list):
                    raise ControllerError("task_response_invalid")
                result[field].extend(values)
        return result


def _validate_evidence(
    config: MigrationConfig, prepared: PreparedMigration, evidence: ImageEvidence
) -> None:
    if (
        prepared.result != "ready"
        or evidence.release_sha != prepared.release_sha
        or evidence.migration_tree_oid != prepared.migration_tree_oid
        or evidence.protocol_version != 1
        or re.fullmatch(r"sha256:[0-9a-f]{64}", evidence.image_digest) is None
        or (evidence.run_id, evidence.run_attempt)
        != (prepared.run_id, prepared.run_attempt)
        or config.release_sha != prepared.release_sha
        or config.github_run_id != f"{prepared.run_id}-{prepared.run_attempt}"
        or config.started_by
        != f"vector-migration-{prepared.run_id}-{prepared.run_attempt}"
        or config.family != f"{config.cluster}-migration"
        or config.network_name != config.family
        or config.container_name != "migration"
    ):
        raise ControllerError("image_or_execution_identity_mismatch")


def _one(values: Sequence[str]) -> str:
    if len(values) != 1 or not values[0]:
        raise ControllerError("migration_network_not_unique")
    return values[0]


def _preflight(
    config: MigrationConfig,
    prepared: PreparedMigration,
    evidence: ImageEvidence,
    client: AwsMigrationControllerClient,
) -> tuple[dict[str, object], str, str]:
    if client.list_active_tasks(config.cluster, config.family):
        raise ControllerError("active_migration_task")
    subnet = _one(client.find_subnet_ids(config.network_name))
    security_group = _one(client.find_security_group_ids(config.network_name))
    base_family = f"{config.cluster}-migration-base"
    source = client.describe_task_definition(base_family)
    raw = source.get("taskDefinition")
    if not isinstance(raw, Mapping) or raw.get("status") != "ACTIVE":
        raise ControllerError("base_definition_not_active")
    definition, _, _ = prepare_task_definition(
        replace(config, family=base_family),
        source,
        command=["python", "-m", "scripts.migration_runner"],
    )
    container = definition["containerDefinitions"][0]
    if (
        any(
            container.get(field)
            for field in (
                "entryPoint",
                "workingDirectory",
                "environmentFiles",
                "mountPoints",
                "volumesFrom",
            )
        )
        or len(container["environment"]) != 4
    ):
        raise ControllerError("base_execution_override")
    if prepared.mode == "contract":
        names = [
            arn.rsplit("/", 1)[-1]
            for arn in client.list_services(config.cluster)
            if arn.rsplit("/", 1)[-1] != "proxy"
        ]
        if not prepared.contract_parent_sha or completed_service_issues(
            config.cluster, prepared.contract_parent_sha, names, client
        ):
            raise ControllerError("contract_application_prerequisite_failed")
    # ひな型の厳格検証が終わるまで、実行要求を環境変数へ混ぜない。
    repository, _, _ = container["image"].rpartition(":")
    container["image"] = f"{repository}@{evidence.image_digest}"
    environment = {
        "MIGRATION_PROTOCOL_VERSION": "1",
        "MIGRATION_MODE": prepared.mode,
        "MIGRATION_TARGET_REVISION": prepared.target_revision,
        "MIGRATION_TREE_OID": prepared.migration_tree_oid,
    }
    if prepared.expected_start_revision is not None:
        environment["MIGRATION_EXPECTED_START_REVISION"] = (
            prepared.expected_start_revision
        )
    container["environment"] = [
        *container["environment"],
        *({"name": key, "value": value} for key, value in environment.items()),
    ]
    definition["family"] = config.family
    return definition, subnet, security_group


def _record(prepared: PreparedMigration) -> LedgerRecord:
    baseline = prepared.baseline.baseline
    if baseline is None:
        raise ControllerError("baseline_unobservable")
    return LedgerRecord(
        schema_version=1,
        release_sha=prepared.release_sha,
        mode=prepared.mode,
        expected_start_revision=prepared.expected_start_revision,
        target_revision=prepared.target_revision,
        migration_tree_oid=prepared.migration_tree_oid,
        github_run_id=prepared.run_id,
        github_run_attempt=prepared.run_attempt,
        baseline_deployment_id=baseline.deployment_id,
        baseline_status_id=baseline.status_id,
    )


def _attempt_path(config: MigrationConfig) -> Path:
    return config.state_file.with_suffix(".ledger.json")


def _save_attempt(config: MigrationConfig, started: StartedAttempt) -> None:
    path = _attempt_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(started)) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_attempt(
    config: MigrationConfig, prepared: PreparedMigration
) -> StartedAttempt | None:
    path = _attempt_path(config)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ControllerError("attempt_file_invalid")
    record = LedgerRecord.from_payload(value.get("record"))
    if record != _record(prepared) or any(
        type(value.get(key)) is not int or value[key] <= 0
        for key in ("deployment_id", "in_progress_status_id")
    ):
        raise ControllerError("attempt_identity_mismatch")
    return StartedAttempt(
        value["deployment_id"], value["in_progress_status_id"], record
    )


def _runtime_valid(
    task: Mapping[str, object], definition_arn: str, evidence: ImageEvidence
) -> bool:
    containers = task.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        return False
    container = containers[0]
    return (
        task.get("taskDefinitionArn") == definition_arn
        and task.get("lastStatus") == "STOPPED"
        and task.get("stopCode") == "EssentialContainerExited"
        and isinstance(container, Mapping)
        and container.get("name") == "migration"
        and container.get("lastStatus") == "STOPPED"
        and type(container.get("exitCode")) is int
        and container.get("exitCode") == 0
        and container.get("imageDigest") == evidence.image_digest
        and str(container.get("image", "")).endswith(f"@{evidence.image_digest}")
    )


def _summary(
    config: MigrationConfig, prepared: PreparedMigration, result: str, reason: str
) -> None:
    lines = [
        "## Migration execution",
        "",
        f"- Result: `{result}` / `{reason}`",
        f"- Approved release: `{prepared.release_sha}`",
        f"- Mode: `{prepared.mode}`",
        f"- Expected start: `{prepared.expected_start_revision or 'unconfirmed'}`",
        f"- Expected target: `{prepared.target_revision}`",
        "- Live revisions and range: migration runner structured CloudWatch logs",
        "- Application services were not updated.",
        "",
    ]
    config.summary_file.parent.mkdir(parents=True, exist_ok=True)
    with config.summary_file.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines))


def run_controller(
    config: MigrationConfig,
    prepared: PreparedMigration,
    evidence: ImageEvidence,
    client: AwsMigrationControllerClient,
    ledger: MigrationLedger,
    *,
    revalidate: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    started: StartedAttempt | None = None
    start_requested = False
    state = None
    completion_attempted = False
    phase = "preflight"
    try:
        control = MigrationTaskControl(config, client)
        _validate_evidence(config, prepared, evidence)
        revalidate()
        definition, subnet, security_group = _preflight(
            config, prepared, evidence, client
        )
        phase = "ledger_begin"
        started = ledger.begin(prepared.baseline, _record(prepared))
        _save_attempt(config, started)
        phase = "task_registration"
        definition_arn = control.register(definition)
        phase = "task_execution"
        start_requested = True
        state = control.start(definition_arn, subnet, security_group)
        task = control.wait(state, monotonic=monotonic, sleep=sleep)
        if not _runtime_valid(task, definition_arn, evidence):
            raise ControllerError("runtime_result_invalid")
        phase = "ledger_success"
        completion_attempted = True
        ledger.finish(started, "success")
        _summary(config, prepared, "success", "migration_verified")
        return 0
    except Exception:
        # API応答やrunner由来の値は診断へ転載せず、失敗した境界だけを返す。
        reason = f"{phase}_failed"
    cleanup_ok = True
    if start_requested:
        cleanup_ok = (
            cleanup_migration(config, client, monotonic=monotonic, sleep=sleep) == 0
        )
    if (
        started is not None
        and not completion_attempted
        and cleanup_ok
        and (not start_requested or state is not None)
    ):
        try:
            ledger.finish(started, "failure")
        except Exception:
            reason = "ledger_failure_unconfirmed"
    if not cleanup_ok or (start_requested and state is None):
        reason = "task_outcome_unconfirmed"
    _summary(config, prepared, "failure", reason)
    print(json.dumps({"result": "failure", "reason": reason}))
    return 2 if phase in {"preflight", "ledger_begin"} else 1


def cleanup_controller(
    config: MigrationConfig,
    prepared: PreparedMigration,
    client: AwsMigrationControllerClient,
    ledger: MigrationLedger,
) -> int:
    if cleanup_migration(config, client) != 0:
        return 1
    try:
        started = _load_attempt(config, prepared)
        if started is None:
            return 0
        current = ledger.read_latest()
        if (
            current.state == "available"
            and current.record == started.record
            and current.baseline is not None
            and current.baseline.deployment_id == started.deployment_id
        ):
            return 0
        # 起動結果不明・task state不在は、停止を試みても完了記録へ変換しない。
        if not config.state_file.exists():
            return 1
        if current.latest_status == "failure" and current.record == started.record:
            return 0
        ledger.finish(started, "failure")
        return 0
    except Exception:
        return 1


def _interrupt(signum: int, frame: object) -> None:
    del signum, frame
    raise ControllerInterrupted()


def main(argv: Sequence[str] | None = None) -> int:
    from migration_image import ImageEvidence
    from migration_prepare import (
        GitReleaseRepository,
        PreparedMigration,
        revalidate_preparation,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "cleanup"))
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--image-evidence", type=Path)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--github-run-id", type=int, required=True)
    parser.add_argument("--github-run-attempt", type=int, required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--timeout-seconds", type=float, default=1200)
    parser.add_argument("--cleanup-timeout-seconds", type=float, default=180)
    args = parser.parse_args(argv)
    try:
        prepared = PreparedMigration.from_dict(json.loads(args.prepared.read_text()))
        if (prepared.run_id, prepared.run_attempt) != (
            args.github_run_id,
            args.github_run_attempt,
        ):
            raise ControllerError("run_identity_mismatch")
        aws_path = shutil.which("aws")
        if aws_path is None:
            raise ControllerError("aws_cli_unavailable")
        config = MigrationConfig(
            cluster=args.cluster,
            release_sha=prepared.release_sha,
            family=f"{args.cluster}-migration",
            container_name="migration",
            network_name=f"{args.cluster}-migration",
            started_by=f"vector-migration-{prepared.run_id}-{prepared.run_attempt}",
            github_run_id=f"{prepared.run_id}-{prepared.run_attempt}",
            state_file=args.state_file,
            summary_file=args.summary_file,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
            cleanup_timeout_seconds=args.cleanup_timeout_seconds,
        )
        github = GitHubCli(args.repo)
        ledger = MigrationLedger(github)
        client = AwsMigrationControllerClient(aws_path)
        if args.operation == "cleanup":
            return cleanup_controller(config, prepared, client, ledger)
        if args.image_evidence is None:
            raise ControllerError("image_evidence_missing")
        evidence = ImageEvidence.from_dict(json.loads(args.image_evidence.read_text()))
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, _interrupt)
        return run_controller(
            config,
            prepared,
            evidence,
            client,
            ledger,
            revalidate=lambda: revalidate_preparation(
                prepared, GitReleaseRepository(args.repo_root), github
            ),
        )
    except (ControllerError, LedgerError, MigrationInputError, OSError, ValueError):
        print(json.dumps({"result": "failure", "reason": "controller_input_invalid"}))
        return 2
    except Exception:
        print(json.dumps({"result": "failure", "reason": "controller_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
