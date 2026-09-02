"""DB適用記録のみを扱い、実DBの検証・承認・実行の排他は呼び出し側に委ねる。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlencode

_OID = re.compile(r"[0-9a-f]{40}")
_REVISION = re.compile(r"[a-zA-Z0-9_.-]{1,127}")


class LedgerError(ValueError):
    """生のAPI応答やコマンド出力を含めない、呼び出し側向けの失敗理由。"""


class GitHubApi(Protocol):
    def get_json(self, path: str) -> object: ...

    def post_json(self, path: str, body: Mapping[str, object]) -> object: ...


@dataclass(frozen=True)
class LedgerNamespace:
    environment: str
    task: str


PRODUCTION_LEDGER = LedgerNamespace("db-migration", "deploy:migrations")
PROBE_LEDGER = LedgerNamespace("db-migration-ledger-probe", "verify:migration-ledger")


def _positive_id(value: object) -> bool:
    return type(value) is int and value > 0


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class LedgerRecord:
    schema_version: int
    release_sha: str
    mode: Literal["verify", "expand", "contract"]
    expected_start_revision: str | None
    target_revision: str
    migration_tree_oid: str
    github_run_id: int
    github_run_attempt: int
    baseline_deployment_id: int | None
    baseline_status_id: int | None

    def __post_init__(self) -> None:
        if not (
            type(self.schema_version) is int
            and self.schema_version == 1
            and _matches(_OID, self.release_sha)
            and self.mode in ("verify", "expand", "contract")
            and _matches(_REVISION, self.target_revision)
            and _matches(_OID, self.migration_tree_oid)
            and _positive_id(self.github_run_id)
            and _positive_id(self.github_run_attempt)
            and (
                self.expected_start_revision is None
                and self.mode == "verify"
                or _matches(_REVISION, self.expected_start_revision)
            )
            and (
                self.baseline_deployment_id is None
                or _positive_id(self.baseline_deployment_id)
            )
            and (
                self.baseline_status_id is None
                or (
                    _positive_id(self.baseline_status_id)
                    and self.baseline_deployment_id is not None
                )
            )
        ):
            raise LedgerError("invalid_ledger_record")

    @classmethod
    def from_payload(cls, payload: object) -> LedgerRecord:
        if not isinstance(payload, dict):
            raise LedgerError("invalid_ledger_record")
        try:
            return cls(**payload)
        except (TypeError, ValueError):
            raise LedgerError("invalid_ledger_record") from None

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Baseline:
    deployment_id: int | None
    status_id: int | None


@dataclass(frozen=True)
class LedgerSnapshot:
    state: Literal["available", "unavailable", "missing"]
    baseline: Baseline | None
    record: LedgerRecord | None = None
    latest_status: str | None = None
    reason: str | None = None

    def allows_rollout(self, target_revision: str, migration_tree_oid: str) -> bool:
        return (
            self.state == "available"
            and self.record is not None
            and self.record.target_revision == target_revision
            and self.record.migration_tree_oid == migration_tree_oid
        )


@dataclass(frozen=True)
class StartedAttempt:
    deployment_id: int
    in_progress_status_id: int
    record: LedgerRecord


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LedgerError("invalid_github_response")
    return value


def _id(value: Mapping[str, object]) -> int:
    number = value.get("id")
    if not _positive_id(number):
        raise LedgerError("invalid_github_response")
    return number


def _created_order(value: Mapping[str, object]) -> tuple[datetime, int]:
    created = value.get("created_at")
    if not isinstance(created, str):
        raise LedgerError("invalid_github_response")
    try:
        timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        raise LedgerError("invalid_github_response") from None
    if timestamp.tzinfo is None:
        raise LedgerError("invalid_github_response")
    return timestamp, _id(value)


class MigrationLedger:
    """baseline再確認はCASではないため、beginからfinishまでの排他を外側で保証する。"""

    def __init__(
        self, client: GitHubApi, namespace: LedgerNamespace = PRODUCTION_LEDGER
    ) -> None:
        self._client = client
        self._namespace = namespace

    def _get(self, path: str) -> object:
        try:
            return self._client.get_json(path)
        except Exception:
            raise LedgerError("github_read_failed") from None

    def _post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        try:
            response = self._client.post_json(path, body)
        except Exception:
            raise LedgerError("github_write_failed") from None
        return _object(response)

    def _list(self, path: str, **filters: str) -> list[Mapping[str, object]]:
        items: list[Mapping[str, object]] = []
        page = 1
        while True:
            query = urlencode({**filters, "per_page": 100, "page": page})
            result = self._get(f"{path}?{query}")
            if not isinstance(result, list):
                raise LedgerError("invalid_github_response")
            items.extend(_object(item) for item in result)
            if len(result) < 100:
                return items
            page += 1

    def _belongs(self, deployment: Mapping[str, object]) -> bool:
        if not all(isinstance(deployment.get(k), str) for k in ("environment", "task")):
            raise LedgerError("invalid_github_response")
        return (
            deployment["environment"] == self._namespace.environment
            and deployment["task"] == self._namespace.task
        )

    def _deployment(self, deployment_id: int) -> Mapping[str, object]:
        deployment = _object(self._get(f"/deployments/{deployment_id}"))
        if _id(deployment) != deployment_id or not self._belongs(deployment):
            raise LedgerError("deployment_identity_mismatch")
        return deployment

    @staticmethod
    def _record(deployment: Mapping[str, object]) -> LedgerRecord:
        record = LedgerRecord.from_payload(deployment.get("payload"))
        if deployment.get("sha") != record.release_sha:
            raise LedgerError("invalid_ledger_record")
        return record

    def read_latest(self) -> LedgerSnapshot:
        try:
            candidates = [
                item
                for item in self._list(
                    "/deployments",
                    environment=self._namespace.environment,
                    task=self._namespace.task,
                )
                if self._belongs(item)
            ]
            if not candidates:
                return LedgerSnapshot("missing", Baseline(None, None))
            deployment_id = _id(max(candidates, key=_created_order))
            deployment = self._deployment(deployment_id)
            statuses = self._list(f"/deployments/{deployment_id}/statuses")
            latest = max(statuses, key=_created_order) if statuses else None
            baseline = Baseline(deployment_id, _id(latest) if latest else None)
            state = latest.get("state") if latest else None
            if state is not None and not isinstance(state, str):
                raise LedgerError("invalid_github_response")
        except LedgerError as exc:
            return LedgerSnapshot("unavailable", None, reason=str(exc))
        try:
            record = self._record(deployment)
        except LedgerError:
            return LedgerSnapshot(
                "unavailable", baseline, latest_status=state, reason="unreadable_record"
            )
        if state != "success":
            return LedgerSnapshot(
                "unavailable", baseline, record, state, "latest_status_not_success"
            )
        return LedgerSnapshot("available", baseline, record, state)

    def begin(self, prepared: LedgerSnapshot, record: LedgerRecord) -> StartedAttempt:
        if prepared.baseline is None:
            raise LedgerError("baseline_unobservable")
        current = self.read_latest()
        if current.baseline is None:
            raise LedgerError("baseline_unobservable")
        if current.baseline != prepared.baseline:
            raise LedgerError("baseline_changed")
        if Baseline(record.baseline_deployment_id, record.baseline_status_id) != (
            prepared.baseline
        ):
            raise LedgerError("record_baseline_mismatch")
        if record.mode != "verify" and (
            current.state != "available"
            or current.record is None
            or record.expected_start_revision != current.record.target_revision
        ):
            raise LedgerError("usable_start_record_required")
        created = self._post(
            "/deployments",
            {
                "ref": record.release_sha,
                "environment": self._namespace.environment,
                "task": self._namespace.task,
                "auto_merge": False,
                "required_contexts": [],
                "production_environment": False,
                "payload": record.to_payload(),
            },
        )
        deployment_id = _id(created)
        if self._record(self._deployment(deployment_id)) != record:
            raise LedgerError("record_round_trip_mismatch")
        progress = self._write_status(deployment_id, "in_progress")
        self._confirm_status(deployment_id, _id(progress), record, "in_progress")
        return StartedAttempt(deployment_id, _id(progress), record)

    def _write_status(self, deployment_id: int, state: str) -> Mapping[str, object]:
        status = self._post(
            f"/deployments/{deployment_id}/statuses",
            {"state": state, "auto_inactive": False},
        )
        _id(status)
        if status.get("state") != state:
            raise LedgerError("status_write_unconfirmed")
        return status

    def _confirm_status(
        self, deployment_id: int, status_id: int, record: LedgerRecord, state: str
    ) -> LedgerSnapshot:
        current = self.read_latest()
        if (
            current.baseline != Baseline(deployment_id, status_id)
            or current.record != record
            or current.latest_status != state
        ):
            raise LedgerError("latest_record_changed_or_unconfirmed")
        return current

    def finish(
        self, started: StartedAttempt, state: Literal["success", "failure"]
    ) -> LedgerSnapshot:
        if state not in ("success", "failure"):
            raise LedgerError("invalid_completion_state")
        self._confirm_status(
            started.deployment_id,
            started.in_progress_status_id,
            started.record,
            "in_progress",
        )
        status = self._write_status(started.deployment_id, state)
        return self._confirm_status(
            started.deployment_id, _id(status), started.record, state
        )


class GitHubCli:
    def __init__(self, repo: str) -> None:
        if re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repo) is None:
            raise LedgerError("invalid_github_repository")
        gh = shutil.which("gh")
        if gh is None:
            raise LedgerError("github_cli_unavailable")
        self._repo = repo
        self._gh = gh

    def get_json(self, path: str) -> object:
        return self._request("GET", path)

    def post_json(self, path: str, body: Mapping[str, object]) -> object:
        return self._request("POST", path, body)

    def _request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> object:
        command = [
            self._gh,
            "api",
            "--hostname",
            "github.com",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            f"repos/{self._repo}{path}",
        ]
        if body is not None:
            command.extend(["--input", "-"])
        try:
            completed = subprocess.run(  # noqa: S603
                command,
                input=json.dumps(body) if body is not None else None,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                raise LedgerError("github_request_failed")
            return json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise LedgerError("github_request_failed") from None


def migration_tree_oid(repo: Path, release_sha: str) -> str:
    """対象commitのmigrationソースを識別し、実DBのschemaを証明する値にはしない。"""
    if not _matches(_OID, release_sha):
        raise LedgerError("invalid_release_sha")
    git = shutil.which("git")
    if git is None:
        raise LedgerError("git_unavailable")
    try:
        result = subprocess.run(  # noqa: S603
            [git, "rev-parse", "--verify", f"{release_sha}:backend/alembic/versions"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        oid = result.stdout.strip()
        if result.returncode != 0 or not _matches(_OID, oid):
            raise LedgerError("migration_tree_unavailable")
        kind = subprocess.run(  # noqa: S603
            [git, "cat-file", "-t", oid],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if kind.returncode != 0 or kind.stdout.strip() != "tree":
            raise LedgerError("migration_tree_unavailable")
        return oid
    except (OSError, subprocess.SubprocessError):
        raise LedgerError("migration_tree_unavailable") from None
