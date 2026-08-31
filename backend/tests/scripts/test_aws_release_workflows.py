"""AWS 本番 release workflow の起動境界を固定する契約テスト。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "aws-app-images.yml"


class _WorkflowLoader(yaml.SafeLoader):
    """GitHub Actionsの`on`を真偽値へ変換せず安全に読むloader。"""


_WorkflowLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _load_workflow(path: Path) -> dict[str, object]:
    loader = _WorkflowLoader(path.read_text(encoding="utf-8"))
    try:
        workflow = loader.get_single_data()
    finally:
        loader.dispose()
    assert isinstance(workflow, dict)
    return workflow


def _release_decision_step() -> dict[str, object]:
    workflow = _load_workflow(_CI_WORKFLOW)
    job = workflow["jobs"]["dispatch-aws-release"]  # type: ignore[index]
    return next(
        step
        for step in job["steps"]  # type: ignore[index]
        if step.get("id") == "decision"
    )


def _run_release_decision(
    tmp_path: Path,
    *,
    ci_gate: str = "success",
    backend: str = "false",
    frontend: str = "false",
    migrations: str = "false",
    aws_infra: str = "false",
    publication_freeze: str = "false",
) -> tuple[str, str]:
    step = _release_decision_step()
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    env = {
        **os.environ,
        "CI_GATE_RESULT": ci_gate,
        "BACKEND_CHANGED": backend,
        "FRONTEND_CHANGED": frontend,
        "MIGRATIONS_CHANGED": migrations,
        "AWS_INFRA_CHANGED": aws_infra,
        "PUBLICATION_FREEZE": publication_freeze,
        "RELEASE_SHA": "a" * 40,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    # リポジトリ管理下のworkflow本文だけを契約テストとして実行する。
    subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", step["run"]],  # type: ignore[list-item]
        check=True,
        env=env,
    )
    return output.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("backend", "frontend"),
    [("true", "false"), ("false", "true")],
)
def test_release_decision_allows_app_change_after_green_ci(
    tmp_path: Path,
    backend: str,
    frontend: str,
) -> None:
    output, summary = _run_release_decision(
        tmp_path,
        backend=backend,
        frontend=frontend,
    )

    assert (output, "dispatch | yes" in summary) == ("eligible=true\n", True)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, "backend / frontend の変更がない"),
        ({"backend": "true", "migrations": "true"}, "migration を含む"),
        ({"backend": "true", "aws_infra": "true"}, "AWS infra 変更を含む"),
        ({"backend": "true", "publication_freeze": ""}, "明示的に false ではない"),
        (
            {"backend": "true", "publication_freeze": "true"},
            "明示的に false ではない",
        ),
        ({"backend": "true", "ci_gate": "failure"}, "CI gate が success ではない"),
    ],
)
def test_release_decision_blocks_unsafe_or_ineligible_change(
    tmp_path: Path,
    overrides: dict[str, str],
    reason: str,
) -> None:
    output, summary = _run_release_decision(tmp_path, **overrides)

    assert (output, reason in summary) == ("eligible=false\n", True)


def test_ci_dispatch_job_has_only_repository_workflow_permission() -> None:
    workflow = _load_workflow(_CI_WORKFLOW)
    changes = workflow["jobs"]["changes"]  # type: ignore[index]
    job = workflow["jobs"]["dispatch-aws-release"]  # type: ignore[index]
    dispatch = next(
        step
        for step in job["steps"]  # type: ignore[index]
        if step.get("name") == "Dispatch AWS app images"
    )
    workflow_text = _CI_WORKFLOW.read_text(encoding="utf-8")

    assert (
        changes["outputs"]["aws_infra"],  # type: ignore[index]
        "infra/aws/**" in changes["steps"][1]["with"]["filters"],  # type: ignore[index]
        ".github/workflows/aws-terraform-apply.yml"
        in changes["steps"][1]["with"]["filters"],  # type: ignore[index]
        job["permissions"],  # type: ignore[index]
        "uses" not in job,
        "github.event_name == 'push'" in job["if"],  # type: ignore[index]
        "github.ref == 'refs/heads/main'" in job["if"],  # type: ignore[index]
        dispatch["if"],
        "gh workflow run aws-app-images.yml" in dispatch["run"],  # type: ignore[index]
        '--raw-field release_sha="$RELEASE_SHA"' in dispatch["run"],  # type: ignore[index]
        "migrate-production:" not in workflow_text,
        "deploy-production:" not in workflow_text,
        "flyctl" not in workflow_text,
        "AWS_PUSH_ROLE_ARN" not in workflow_text,
        "AWS_ROLLOUT_ROLE_ARN" not in workflow_text,
    ) == (
        "${{ steps.filter.outputs.aws_infra }}",
        True,
        True,
        {"actions": "write", "contents": "read"},
        True,
        True,
        True,
        "${{ steps.decision.outputs.eligible == 'true' }}",
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    )


def test_aws_release_workflow_pins_sha_and_keeps_approval_boundary() -> None:
    workflow = _load_workflow(_RELEASE_WORKFLOW)
    workflow_text = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    release_input = workflow["on"]["workflow_dispatch"]["inputs"][  # type: ignore[index]
        "release_sha"
    ]
    jobs = workflow["jobs"]  # type: ignore[index]

    assert (
        release_input,
        workflow["env"]["RELEASE_SHA"],  # type: ignore[index]
        workflow["concurrency"],  # type: ignore[index]
        jobs["rollout"]["environment"],  # type: ignore[index]
        "^[0-9a-f]{40}$" in workflow_text,
        workflow_text.count("ref: ${{ env.RELEASE_SHA }}"),
        "IMAGE_TAG: ${{ env.RELEASE_SHA }}" in workflow_text,
        "${{ github.sha }}" not in workflow_text,
        "workflow_call" not in workflow["on"],  # type: ignore[operator]
    ) == (
        {
            "description": "空なら選択した ref の commit SHA を使う。",
            "required": "false",
            "type": "string",
        },
        "${{ inputs.release_sha || github.sha }}",
        {"group": "aws-app-images", "cancel-in-progress": "false"},
        "production",
        True,
        2,
        True,
        True,
        True,
    )
