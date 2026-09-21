"""Lambdaイメージ更新の対象選択・更新・収束検証の契約。"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "rollout_lambda_images.py"
_SHA = "a" * 40
_REGISTRY = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com"
_BACKEND = f"{_REGISTRY}/vector/backend"
_OLD = "sha256:" + "0" * 64
_NEW = "sha256:" + "1" * 64

pytestmark = pytest.mark.unit


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rollout_lambda_images", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rollout = _load_module()


@dataclass
class FakeFunction:
    """更新要求を受けると、指定回数のpoll後に収束または失敗する関数。"""

    name: str
    digest: str = _OLD
    repository_uri: str = _BACKEND
    package_type: str = "Image"
    state: str = "Active"
    polls_until_done: int = 1
    fails_with: str = ""
    update_status: str = "Successful"
    pending_digest: str = ""
    remaining_polls: int = 0


@dataclass
class FakeClient:
    functions: list[FakeFunction]
    release_digest: str | None = _NEW
    update_error: dict[str, str] = field(default_factory=dict)
    list_error: str = ""
    updates: list[tuple[str, str]] = field(default_factory=list)

    def resolve_image_digest(self, repository: str, image_tag: str) -> str | None:
        assert (repository, image_tag) == ("vector/backend", _SHA)
        return self.release_digest

    def list_functions(self) -> Sequence[Mapping[str, object]]:
        if self.list_error:
            raise rollout.AwsCliError(self.list_error)
        return [
            {"FunctionName": item.name, "PackageType": item.package_type}
            for item in self.functions
        ]

    def get_function(self, name: str) -> Mapping[str, object]:
        item = next(f for f in self.functions if f.name == name)
        if item.update_status == "InProgress":
            item.remaining_polls -= 1
            if item.remaining_polls <= 0:
                if item.fails_with:
                    item.update_status = "Failed"
                else:
                    item.update_status = "Successful"
                    item.digest = item.pending_digest
        return {
            "Configuration": {
                "State": item.state,
                "LastUpdateStatus": item.update_status,
                "LastUpdateStatusReason": item.fails_with
                if item.update_status == "Failed"
                else None,
            },
            "Code": {
                "ImageUri": f"{item.repository_uri}@{item.digest}",
                "ResolvedImageUri": f"{item.repository_uri}@{item.digest}",
            },
        }

    def update_function_image(self, name: str, image_uri: str) -> None:
        if name in self.update_error:
            raise rollout.AwsCliError(self.update_error[name])
        self.updates.append((name, image_uri))
        item = next(f for f in self.functions if f.name == name)
        item.update_status = "InProgress"
        item.pending_digest = image_uri.rpartition("@")[2]
        # 対象選択時のget_functionとは別に、更新後のpoll回数だけを数える。
        item.remaining_polls = item.polls_until_done


def _run(
    tmp_path: Path, client: FakeClient, *, timeout: float = 60
) -> tuple[int, str, list[float]]:
    summary = tmp_path / "summary.md"
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    code = rollout.rollout_lambda_images(
        rollout.RolloutConfig(
            name_prefix="vector",
            repository="vector/backend",
            image_tag=_SHA,
            timeout_seconds=timeout,
            poll_seconds=10,
            summary_file=summary,
        ),
        client,
        monotonic=lambda: now[0],
        sleep=sleep,
    )
    return code, summary.read_text() if summary.exists() else "", sleeps


def test_every_backend_function_is_updated_to_the_release_digest(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        [
            FakeFunction("vector-outbox-relay", polls_until_done=2),
            FakeFunction("vector-curation-consumer"),
            FakeFunction("other-team-function"),
        ]
    )

    code, summary, sleeps = _run(tmp_path, client)

    assert code == rollout.SUCCESS
    assert client.updates == [
        ("vector-curation-consumer", f"{_BACKEND}@{_NEW}"),
        ("vector-outbox-relay", f"{_BACKEND}@{_NEW}"),
    ]
    assert sleeps == [10]
    assert "Result: **success**" in summary
    assert "other-team-function" not in summary


def test_function_already_on_the_release_digest_is_not_updated(tmp_path: Path) -> None:
    client = FakeClient(
        [
            FakeFunction("vector-outbox-relay", digest=_NEW),
            FakeFunction("vector-curation-consumer"),
        ]
    )

    code, summary, _ = _run(tmp_path, client)

    assert code == rollout.SUCCESS
    assert [name for name, _ in client.updates] == ["vector-curation-consumer"]
    assert "| `vector-outbox-relay` | unchanged |" in summary


def test_idle_inactive_function_on_the_release_digest_counts_as_converged(
    tmp_path: Path,
) -> None:
    client = FakeClient([FakeFunction("vector-cleanup", digest=_NEW, state="Inactive")])

    code, _, sleeps = _run(tmp_path, client)

    assert (code, client.updates, sleeps) == (rollout.SUCCESS, [], [])


def test_missing_release_image_updates_nothing(tmp_path: Path) -> None:
    client = FakeClient([FakeFunction("vector-outbox-relay")], release_digest=None)

    code, summary, _ = _run(tmp_path, client)

    assert (code, client.updates) == (rollout.CONTRACT_FAILURE, [])
    assert "Release image is missing" in summary


def test_no_target_function_is_a_failure(tmp_path: Path) -> None:
    client = FakeClient([FakeFunction("other-team-function")])

    code, summary, _ = _run(tmp_path, client)

    assert (code, client.updates) == (rollout.CONTRACT_FAILURE, [])
    assert "No Lambda function to roll out" in summary


def test_function_on_another_repository_is_reported_but_not_updated(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        [
            FakeFunction("vector-outbox-relay"),
            FakeFunction("vector-tool", repository_uri=f"{_REGISTRY}/vector/tools"),
            FakeFunction("vector-zip", package_type="Zip"),
        ]
    )

    code, summary, _ = _run(tmp_path, client)

    assert code == rollout.SUCCESS
    assert [name for name, _ in client.updates] == ["vector-outbox-relay"]
    assert "対象外 (backend以外のイメージ): `vector-tool`" in summary
    assert "vector-zip" not in summary


def test_failed_update_reports_reason_and_keeps_other_results(tmp_path: Path) -> None:
    client = FakeClient(
        [
            FakeFunction("vector-curation-consumer"),
            FakeFunction(
                "vector-outbox-relay",
                fails_with="ImageAccessDenied for account 123456789012",
            ),
        ]
    )

    code, summary, _ = _run(tmp_path, client)

    assert code == rollout.ROLLOUT_FAILURE
    assert "Lambda image update failed" in summary
    assert "ImageAccessDenied for account &lt;ACCOUNT_ID&gt;" in summary
    assert (
        "| `vector-curation-consumer` | update | Active | Successful | yes |" in summary
    )
    assert "123456789012" not in summary


def test_rollout_times_out_when_a_function_does_not_converge(tmp_path: Path) -> None:
    client = FakeClient([FakeFunction("vector-outbox-relay", polls_until_done=99)])

    code, summary, sleeps = _run(tmp_path, client, timeout=20)

    assert code == rollout.ROLLOUT_FAILURE
    assert sleeps == [10, 10]
    assert "Lambda image rollout timed out" in summary
    assert "rollout_timeout" in summary


def test_rejected_update_request_stops_and_keeps_earlier_results(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        [
            FakeFunction("vector-a-consumer"),
            FakeFunction("vector-b-consumer"),
            FakeFunction("vector-c-consumer"),
        ],
        update_error={"vector-b-consumer": "AccessDeniedException"},
    )

    code, summary, _ = _run(tmp_path, client)

    assert code == rollout.CONTRACT_FAILURE
    assert [name for name, _ in client.updates] == ["vector-a-consumer"]
    assert "| `vector-b-consumer` | request_failed |" in summary
    assert "AccessDeniedException" in summary


def test_aws_api_failure_before_any_update_is_a_contract_failure(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        [FakeFunction("vector-outbox-relay")], list_error="ThrottlingException"
    )

    code, summary, _ = _run(tmp_path, client)

    assert (code, client.updates) == (rollout.CONTRACT_FAILURE, [])
    assert "AWS API error" in summary


@pytest.mark.parametrize(
    "arguments",
    [
        ["--image-tag", "latest"],
        ["--name-prefix", "Vector Prod"],
        ["--timeout-seconds", "0"],
    ],
)
def test_invalid_arguments_stop_before_calling_aws(
    arguments: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    defaults = {
        "--name-prefix": "vector",
        "--repository": "vector/backend",
        "--image-tag": _SHA,
        "--timeout-seconds": "60",
        "--poll-seconds": "10",
        "--summary-file": str(tmp_path / "summary.md"),
    }
    defaults[arguments[0]] = arguments[1]

    code = rollout.main([part for pair in defaults.items() for part in pair])

    assert code == rollout.CONTRACT_FAILURE
    assert "::error::" in capsys.readouterr().out
