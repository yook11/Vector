"""AWS 本番 release workflow の起動境界を固定する契約テスト。"""

from __future__ import annotations

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


def test_app_test_jobs_run_only_on_pull_request() -> None:
    jobs = _load_workflow(_CI_WORKFLOW)["jobs"]  # type: ignore[index]
    backend_unit = jobs["backend-unit"]["if"]  # type: ignore[index]
    backend_integration = jobs["backend-integration"]["if"]  # type: ignore[index]
    frontend = jobs["frontend"]["if"]  # type: ignore[index]
    e2e = jobs["frontend-e2e-smoke"]["if"]  # type: ignore[index]

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
    ) == (True,) * 13


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
        "github.ref == 'refs/heads/main'",
        "${{ secrets.AWS_APPLY_ROLE_ARN }}",
        True,
        True,
        True,
        True,
        {
            "group": "vector-production-change",
            "cancel-in-progress": "false",
            "queue": "max",
        },
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


def test_release_requires_main_approval_read_only_ledger_and_shared_exclusion() -> None:
    app = _load_workflow(_RELEASE_WORKFLOW)
    migration = _load_workflow(_REPO_ROOT / ".github/workflows/aws-db-migration.yml")
    apply = _load_workflow(_APPLY_WORKFLOW)
    jobs = app["jobs"]
    assert app["on"] == {"workflow_dispatch": None}
    assert app["env"]["RELEASE_SHA"] == "${{ github.sha }}"
    assert app["permissions"] == {}
    assert set(jobs) == {"push", "rollout"}
    assert jobs["rollout"]["needs"] == "push"
    assert jobs["rollout"]["environment"] == "production-rollout"
    assert jobs["rollout"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "deployments": "read",
        "id-token": "write",
    }
    for name, job in jobs.items():
        assert "github.ref == 'refs/heads/main'" in job["if"]
        assert "github.event_name == 'workflow_dispatch'" in job["if"]
        credentials = next(
            i
            for i, step in enumerate(job["steps"])
            if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")
        )
        gate = next(
            i
            for i, step in enumerate(job["steps"])
            if "check_app_release.py" in step.get("run", "")
        )
        assert gate < credentials
        assert job["steps"][credentials]["with"]["role-to-assume"] == (
            "${{ secrets.AWS_PUSH_ROLE_ARN }}"
            if name == "push"
            else "${{ secrets.AWS_ROLLOUT_ROLE_ARN }}"
        )
        checkouts = [
            step["with"]
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        ]
        assert any(
            item["ref"] == "${{ github.workflow_sha }}"
            and item["path"] == ".rollout-control"
            for item in checkouts
        )
        terraform = next(step["run"] for step in job["steps"] if step.get("id") == "tf")
        assert ".rollout-control/infra/aws/variables.tf" in terraform
    production_jobs = [
        jobs["rollout"],
        migration["jobs"]["migrate"],
        apply["jobs"]["apply"],
    ]
    assert all(
        job["concurrency"]
        == {
            "group": "vector-production-change",
            "cancel-in-progress": "false",
            "queue": "max",
        }
        for job in production_jobs
    )
    assert "concurrency" not in app and "concurrency" not in jobs["push"]


def test_old_release_routes_are_absent_but_migration_ci_remains() -> None:
    ci = _load_workflow(_CI_WORKFLOW)
    app = _load_workflow(_RELEASE_WORKFLOW)
    text = _CI_WORKFLOW.read_text() + _RELEASE_WORKFLOW.read_text()
    assert all(
        value not in text
        for value in (
            "PUBLICATION_FREEZE",
            "force_migration",
            "decide_ecs_migration",
            "run_ecs_migration",
            "run_production_migration",
            "AWS_MIGRATION_ROLE_ARN",
            "aws ecs run-task",
            "gh workflow run",
            "deployments: write",
        )
    )
    assert "migration-check" in ci["jobs"]["ci-gate"]["needs"]
    migration = ci["jobs"]["migration-check"]
    steps = migration["steps"]
    classify = next(
        step for step in steps if "scripts.migration_change_gate" in step.get("run", "")
    )
    assert "set -euo pipefail" in classify["run"]
    assert classify["env"] == {
        "MIGRATION_EVENT": "${{ github.event_name }}",
        "MIGRATION_BASE_SHA": (
            "${{ github.event.pull_request.base.sha || github.event.before }}"
        ),
        "MIGRATION_HEAD_SHA": (
            "${{ github.event.pull_request.head.sha || github.sha }}"
        ),
    }
    assert steps.index(classify) < next(
        index
        for index, step in enumerate(steps)
        if "alembic upgrade" in step.get("run", "")
    )
    assert (
        next(
            step["with"]["fetch-depth"]
            for step in steps
            if step.get("uses", "").startswith("actions/checkout@")
        )
        == 0
    )
    assert migration.get("continue-on-error", "false") == "false"
    filters = yaml.safe_load(
        next(
            step["with"]["filters"]
            for step in ci["jobs"]["changes"]["steps"]
            if "filters" in step.get("with", {})
        )
    )
    assert {key: filters[key] for key in ("backend", "frontend", "migrations")} == {
        "backend": ["backend/**"],
        "frontend": ["frontend/**"],
        "migrations": ["backend/alembic/versions/**"],
    }
    assert all(
        job.get("permissions", {}).get("actions") != "write"
        for job in ci["jobs"].values()
    )
    assert "inputs" not in (app["on"]["workflow_dispatch"] or {})
    assert not any(
        path.exists()
        for path in (
            _REPO_ROOT / ".github/scripts/decide_ecs_migration.py",
            _REPO_ROOT / ".github/scripts/run_ecs_migration.py",
            _REPO_ROOT / "backend/scripts/run_production_migration.py",
        )
    )
