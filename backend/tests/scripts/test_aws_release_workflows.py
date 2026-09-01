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
_APPLY_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "aws-terraform-apply.yml"
_SCHEMATHESIS_WORKFLOW = (
    _REPO_ROOT / ".github" / "workflows" / "schemathesis-nightly.yml"
)

pytestmark = pytest.mark.unit


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
    migration_decision: str = "none",
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
        "MIGRATION_DECISION": migration_decision,
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
    ("backend", "frontend", "migration_decision"),
    [
        ("true", "false", "none"),
        ("false", "true", "none"),
        ("true", "false", "expand"),
    ],
)
def test_release_decision_allows_app_change_after_green_ci(
    tmp_path: Path,
    backend: str,
    frontend: str,
    migration_decision: str,
) -> None:
    output, summary = _run_release_decision(
        tmp_path,
        backend=backend,
        frontend=frontend,
        migration_decision=migration_decision,
    )

    assert (output, "dispatch | yes" in summary) == ("eligible=true\n", True)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, "backend / frontend の変更がない"),
        (
            {"backend": "true", "migration_decision": "manual"},
            "contract または mixed migration",
        ),
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
    migration_job = workflow["jobs"]["migration-check"]  # type: ignore[index]
    migration_environment = migration_job["env"]  # type: ignore[index]
    gate = workflow["jobs"]["ci-gate"]  # type: ignore[index]

    assert (
        changes["outputs"]["aws_infra"],  # type: ignore[index]
        "infra/aws/**" in changes["steps"][1]["with"]["filters"],  # type: ignore[index]
        ".github/scripts/**" in changes["steps"][1]["with"]["filters"],  # type: ignore[index]
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
        migration_job["outputs"]["decision"],  # type: ignore[index]
        "MIGRATION_DATABASE_URL" in migration_environment,
        "DATABASE_URL" not in migration_environment,
        "BFF_JWT_SIGNING_SECRET" not in migration_environment,
        "REVALIDATE_BEARER_SECRET" not in migration_environment,
        gate["outputs"]["migration_decision"],  # type: ignore[index]
        "--decision-output" in workflow_text,
    ) == (
        "${{ steps.filter.outputs.aws_infra }}",
        True,
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
        "${{ steps.classify.outputs.decision }}",
        True,
        True,
        True,
        True,
        "${{ steps.migration-decision.outputs.value }}",
        True,
    )


def test_app_test_jobs_run_only_on_pull_request() -> None:
    jobs = _load_workflow(_CI_WORKFLOW)["jobs"]  # type: ignore[index]
    backend_unit = jobs["backend-unit"]["if"]  # type: ignore[index]
    backend_integration = jobs["backend-integration"]["if"]  # type: ignore[index]
    frontend = jobs["frontend"]["if"]  # type: ignore[index]
    e2e = jobs["frontend-e2e-smoke"]["if"]  # type: ignore[index]
    dispatch = jobs["dispatch-aws-release"]["if"]  # type: ignore[index]

    assert (
        "github.event_name == 'pull_request'" in backend_unit,
        "github.event_name == 'push'" not in backend_unit,
        "needs.changes.outputs.backend == 'true'" in backend_unit,
        "needs.changes.outputs.ci == 'true'" in backend_unit,
        "github.event_name == 'pull_request'" in backend_integration,
        "github.event_name == 'push'" not in backend_integration,
        "needs.changes.outputs.backend == 'true'" in backend_integration,
        "github.event_name == 'pull_request'" in frontend,
        "github.event_name == 'push'" not in frontend,
        "needs.changes.outputs.frontend == 'true'" in frontend,
        "github.event_name == 'pull_request'" in e2e,
        "github.event_name == 'push'" not in e2e,
        "needs.changes.outputs.e2e == 'true'" in e2e,
        "github.event_name == 'push'" in dispatch,
    ) == (True,) * 14


def test_terraform_apply_runs_plan_and_apply_after_production_approval() -> None:
    workflow = _load_workflow(_APPLY_WORKFLOW)
    workflow_text = _APPLY_WORKFLOW.read_text(encoding="utf-8")
    jobs = workflow["jobs"]  # type: ignore[index]
    apply = jobs["apply"]  # type: ignore[index]
    credentials = next(
        step
        for step in apply["steps"]  # type: ignore[index]
        if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")
    )
    plan_apply = next(
        step
        for step in apply["steps"]  # type: ignore[index]
        if step.get("name") == "Terraform plan and apply"
    )

    assert (
        list(jobs),
        apply.get("environment"),
        apply.get("needs"),
        apply.get("if"),
        credentials["with"]["role-to-assume"],
        "AWS_PLAN_ROLE_ARN" not in workflow_text,
        "terraform plan" in plan_apply["run"],
        "terraform apply" in plan_apply["run"],
        "-out=tfplan" in plan_apply["run"],
        apply["concurrency"],
    ) == (
        ["apply"],
        "production",
        None,
        None,
        "${{ secrets.AWS_APPLY_ROLE_ARN }}",
        True,
        True,
        True,
        True,
        {"group": "aws-terraform-apply", "cancel-in-progress": "false"},
    )


@pytest.mark.parametrize("path", [_CI_WORKFLOW, _SCHEMATHESIS_WORKFLOW])
def test_every_workflow_alembic_invocation_has_explicit_migration_url(
    path: Path,
) -> None:
    workflow = _load_workflow(path)
    for job_name, job in workflow["jobs"].items():  # type: ignore[union-attr]
        job_environment = job.get("env", {})
        for step in job.get("steps", []):
            if "uv run alembic " not in str(step.get("run", "")):
                continue
            environment = {**job_environment, **step.get("env", {})}
            assert environment.get("MIGRATION_DATABASE_URL"), (
                f"{path.name}:{job_name} must set MIGRATION_DATABASE_URL"
            )


def test_aws_release_workflow_pins_sha_and_keeps_approval_boundary() -> None:
    workflow = _load_workflow(_RELEASE_WORKFLOW)
    workflow_text = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    release_input = workflow["on"]["workflow_dispatch"]["inputs"][  # type: ignore[index]
        "release_sha"
    ]
    jobs = workflow["jobs"]  # type: ignore[index]
    push_steps = jobs["push"]["steps"]  # type: ignore[index]
    rollout_steps = jobs["rollout"]["steps"]  # type: ignore[index]
    migration = next(
        step for step in rollout_steps if step.get("name") == "Run production migration"
    )
    migration_credentials = next(
        step
        for step in rollout_steps
        if step.get("name") == "Configure AWS migration credentials"
    )
    rollout_credentials = next(
        step
        for step in rollout_steps
        if step.get("name") == "Configure AWS rollout credentials"
    )
    cleanup = next(
        step
        for step in rollout_steps
        if step.get("name") == "Stop the exact migration task after failure"
    )
    rollout_step = next(
        step for step in rollout_steps if step.get("name") == "Roll out services"
    )
    verifier_checkout = next(
        step
        for step in rollout_steps
        if step.get("name") == "Check out rollout verifier"
    )
    verifier = next(
        step
        for step in rollout_steps
        if step.get("name") == "Verify rollout completion"
    )

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
        "aws ecs wait services-stable" not in workflow_text,
        "python3 .rollout-control/.github/scripts/verify_ecs_rollout.py"
        in verifier["run"],
        "--poll-seconds 15" in verifier["run"],
        "--timeout-seconds 1200" in verifier["run"],
        verifier["env"]["EXPECTED_TASK_DEFINITIONS"],
        "grep -vx 'proxy'" in rollout_step["run"],
        "expected_task_definitions" in rollout_step["run"],
        'echo "expected-file=$expected_file"' in rollout_step["run"],
        'echo "rollout-started-at=$rollout_started_at"' in rollout_step["run"],
        rollout_step["run"].index('echo "expected-file=$expected_file"')
        < rollout_step["run"].index("while read -r svc arn"),
        "always()" in verifier["if"],
        verifier_checkout["with"],
        jobs["rollout"]["timeout-minutes"],  # type: ignore[index]
        sum(job.get("environment") == "production" for job in jobs.values()),
        migration_credentials["with"]["role-to-assume"],
        rollout_credentials["with"]["role-to-assume"],
        rollout_steps.index(migration_credentials)
        < rollout_steps.index(migration)
        < rollout_steps.index(rollout_credentials)
        < rollout_steps.index(rollout_step),
        "if" not in rollout_credentials,
        "if" not in rollout_step,
        migration["timeout-minutes"],
        "run_ecs_migration.py" in migration["run"],
        "--poll-seconds 15" in migration["run"],
        "--timeout-seconds 1200" in migration["run"],
        "--cleanup" in cleanup["run"],
        cleanup["timeout-minutes"],
        "steps.decision.outputs.run == 'true'" in cleanup["if"],
        "steps.migration.outcome == 'failure'" in cleanup["if"],
        "steps.migration.outcome == 'cancelled'" in cleanup["if"],
        "failure() || cancelled()" in cleanup["if"],
        any(
            step.get("name") == "Require the production migration runtime"
            for step in push_steps
        ),
        "import scripts.run_production_migration"
        in next(step for step in push_steps if step.get("name") == "Build and push")[
            "run"
        ],
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
        True,
        True,
        True,
        True,
        "${{ steps.rollout.outputs.expected-file }}",
        True,
        True,
        True,
        True,
        True,
        True,
        {
            "ref": "${{ github.workflow_sha }}",
            "path": ".rollout-control",
            "persist-credentials": "false",
        },
        55,
        1,
        "${{ secrets.AWS_MIGRATION_ROLE_ARN }}",
        "${{ secrets.AWS_ROLLOUT_ROLE_ARN }}",
        True,
        True,
        True,
        25,
        True,
        True,
        True,
        True,
        3,
        True,
        True,
        True,
        True,
        True,
        True,
    )


def test_rollout_skips_noop_migration_fargate() -> None:
    workflow = _load_workflow(_RELEASE_WORKFLOW)
    workflow_text = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    jobs = workflow["jobs"]  # type: ignore[index]
    rollout = jobs["rollout"]  # type: ignore[index]
    rollout_steps = rollout["steps"]  # type: ignore[index]
    names = [step.get("name") or "" for step in rollout_steps]
    release_checkout = next(
        step
        for step in rollout_steps
        if str(step.get("uses", "")).startswith("actions/checkout@")
        and step.get("with", {}).get("path") != ".rollout-control"
    )
    decision = next(step for step in rollout_steps if step.get("id") == "decision")
    migration_credentials = next(
        step
        for step in rollout_steps
        if step.get("name") == "Configure AWS migration credentials"
    )
    migration = next(
        step for step in rollout_steps if step.get("name") == "Run production migration"
    )
    record = next(
        step
        for step in rollout_steps
        if step.get("name") == "Record successful db-migration"
    )
    cleanup_credentials = next(
        step
        for step in rollout_steps
        if step.get("name") == "Configure AWS migration credentials for cleanup"
    )
    cleanup = next(
        step
        for step in rollout_steps
        if step.get("name") == "Stop the exact migration task after failure"
    )
    script = (_REPO_ROOT / ".github" / "scripts" / "decide_ecs_migration.py").read_text(
        encoding="utf-8"
    )

    assert (
        workflow["permissions"],
        jobs["push"].get("permissions"),
        rollout["permissions"],
        "deployments:" not in workflow_text.split("jobs:", 1)[0],
        release_checkout["with"]["fetch-depth"],
        "python3 .rollout-control/.github/scripts/decide_ecs_migration.py decide"
        in decision["run"],
        "python3 .rollout-control/.github/scripts/decide_ecs_migration.py record"
        in record["run"],
        names.index("Check out rollout verifier")
        < names.index("Decide whether to run production migration")
        < names.index("Configure AWS migration credentials"),
        migration_credentials["if"],
        migration["if"],
        record["if"],
        workflow["on"]["workflow_dispatch"]["inputs"]["force_migration"],  # type: ignore[index]
        '"auto_merge": False' in script,
        '"required_contexts": []' in script,
        '"production_environment": False' in script,
        "steps.decision.outputs.run == 'true'" in cleanup_credentials["if"],
        "steps.migration.outcome == 'failure'" in cleanup_credentials["if"],
        "steps.migration.outcome == 'cancelled'" in cleanup_credentials["if"],
        "failure() || cancelled()" in cleanup_credentials["if"],
        "steps.decision.outputs.run == 'true'" in cleanup["if"],
        "steps.migration.outcome == 'failure'" in cleanup["if"],
        "steps.migration.outcome == 'cancelled'" in cleanup["if"],
        "failure() || cancelled()" in cleanup["if"],
    ) == (
        {"id-token": "write", "contents": "read"},
        None,
        {
            "contents": "read",
            "id-token": "write",
            "deployments": "write",
        },
        True,
        0,
        True,
        True,
        True,
        "${{ steps.decision.outputs.run == 'true' }}",
        "${{ steps.decision.outputs.run == 'true' }}",
        "${{ steps.decision.outputs.run == 'true' }}",
        {
            "description": "true なら alembic 差分が無くても migration task を起動する。",
            "required": "false",
            "type": "boolean",
            "default": "false",
        },
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
