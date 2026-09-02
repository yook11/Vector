"""Migration専用workflowの承認・実行境界を固定する。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _ROOT / ".github/workflows/aws-db-migration.yml"

pytestmark = pytest.mark.unit


def _workflow() -> dict:
    loader = yaml.BaseLoader(_WORKFLOW.read_text(encoding="utf-8"))
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _aws_steps(job: dict) -> list[dict]:
    return [
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")
    ]


def test_only_approved_migration_job_has_ledger_write_and_migration_role() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    expected_permissions = {
        "prepare": {"contents": "read", "actions": "read", "deployments": "read"},
        "build": {"contents": "read", "id-token": "write"},
        "migrate": {
            "contents": "read",
            "actions": "read",
            "deployments": "write",
            "id-token": "write",
        },
    }
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]

    assert (
        set(workflow["on"]),
        set(inputs),
        all(value["required"] == "true" for value in inputs.values()),
        all("default" not in value for value in inputs.values()),
        inputs["mode"]["options"],
        workflow["permissions"],
        {name: job["permissions"] for name, job in jobs.items()},
        {
            name: [step["with"]["role-to-assume"] for step in _aws_steps(job)]
            for name, job in jobs.items()
        },
        {name: job.get("environment") for name, job in jobs.items()},
        all(
            "github.event_name == 'workflow_dispatch'" in job["if"]
            and "github.ref == 'refs/heads/main'" in job["if"]
            for job in jobs.values()
        ),
        jobs["build"]["needs"],
        jobs["migrate"]["needs"],
    ) == (
        {"workflow_dispatch"},
        {"release_sha", "mode"},
        True,
        True,
        ["expand", "contract", "verify"],
        {},
        expected_permissions,
        {
            "prepare": [],
            "build": ["${{ secrets.AWS_PUSH_ROLE_ARN }}"],
            "migrate": ["${{ secrets.AWS_MIGRATION_ROLE_ARN }}"] * 2,
        },
        {"prepare": None, "build": None, "migrate": "production-migration"},
        True,
        "prepare",
        ["prepare", "build"],
    )


@pytest.mark.parametrize(
    ("sha", "mode", "exit_code"),
    [
        ("a" * 40, "expand", 0),
        ("a" * 40, "contract", 0),
        ("a" * 40, "verify", 0),
        ("main", "verify", 2),
        ("a" * 40, "", 2),
        ("a" * 40, "verify; exit 0", 2),
    ],
)
def test_dispatch_input_is_rejected_before_checkout_or_credentials(
    sha: str, mode: str, exit_code: int
) -> None:
    jobs = _workflow()["jobs"]
    results = []
    for job in jobs.values():
        validation = job["steps"][0]
        assert validation["name"] == "Validate dispatch input"
        result = subprocess.run(  # noqa: S603
            ["/bin/bash", "-c", validation["run"]],
            check=False,
            capture_output=True,
            env={**os.environ, "RELEASE_SHA": sha, "MIGRATION_MODE": mode},
        )
        results.append((result.returncode, result.stdout, result.stderr))

    assert results == [(exit_code, b"", b"")] * len(jobs)


def test_control_and_artifacts_are_bound_to_the_workflow_and_current_run() -> None:
    jobs = _workflow()["jobs"]
    for name, job in jobs.items():
        control = _step(job, "Check out trusted migration control")["with"]
        release = _step(job, "Check out release data")["with"]
        install = _step(job, "Install trusted control dependencies")["run"]
        assert (
            control["ref"],
            control["path"],
            control["persist-credentials"],
            release["ref"],
            release["fetch-depth"],
            release["persist-credentials"],
            "--project .migration-control/backend" in install,
            "--frozen" in install,
        ) == (
            "${{ github.workflow_sha }}",
            ".migration-control",
            "false",
            "${{ inputs.release_sha }}",
            "0",
            "false",
            True,
            True,
        )
        if name != "prepare":
            validation = _step(job, "Validate prepared input before AWS credentials")
            assert job["steps"].index(validation) < job["steps"].index(
                _aws_steps(job)[0]
            )
            for binding in (
                '--release-sha "$RELEASE_SHA"',
                '--mode "$MIGRATION_MODE"',
                '--run-id "$GITHUB_RUN_ID"',
                '--run-attempt "$GITHUB_RUN_ATTEMPT"',
            ):
                assert binding in validation["run"]

    downloads = {
        name: [
            step["with"]
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/download-artifact@")
        ]
        for name, job in jobs.items()
    }
    assert {
        name: [download["artifact-ids"] for download in values]
        for name, values in downloads.items()
    } == {
        "prepare": [],
        "build": ["${{ needs.prepare.outputs.artifact-id }}"],
        "migrate": [
            "${{ needs.prepare.outputs.artifact-id }}",
            "${{ needs.build.outputs.artifact-id }}",
        ],
    }
    assert all(
        download["digest-mismatch"] == "error"
        and not {"github-token", "repository", "run-id", "name"} & download.keys()
        for values in downloads.values()
        for download in values
    )


def test_build_verifies_both_new_and_reused_backend_before_approval() -> None:
    jobs = _workflow()["jobs"]
    build = jobs["build"]
    publish = _step(build, "Build and push backend")
    verify = _step(build, "Verify actual image protocol and migration tree")
    artifact = next(
        step for step in build["steps"] if step.get("id") == "image-artifact"
    )
    prepared = _step(
        jobs["prepare"], "Prepare expected migration range without DB access"
    )

    assert (
        build["runs-on"],
        publish["if"],
        "--platform linux/arm64" in publish["run"],
        "release/backend" in publish["run"],
        verify.get("if"),
        'image="$REGISTRY/$REPOSITORY@$digest"' in verify["run"],
        'docker pull "$image"' in verify["run"],
        ".migration-control/.github/scripts/migration_image.py" in verify["run"],
        build["steps"].index(publish) < build["steps"].index(verify),
        build["steps"].index(verify) < build["steps"].index(artifact),
        "needs.prepare.outputs.result == 'ready'" in build["if"],
        "needs.prepare.outputs.result == 'ready'" in jobs["migrate"]["if"],
        '--summary-file "$GITHUB_STEP_SUMMARY"' in prepared["run"],
        "MIGRATION_DATABASE_URL" not in str(jobs["prepare"]),
    ) == (
        "ubuntu-24.04-arm",
        "steps.existing.outputs.found == 'false'",
        True,
        True,
        None,
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


def test_only_migration_execution_is_serialized_and_failure_has_owned_cleanup() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    migration = _step(jobs["migrate"], "Run approved migration without app rollout")
    cleanup = _step(jobs["migrate"], "Stop only the task owned by this attempt")
    workflow_text = _WORKFLOW.read_text(encoding="utf-8")

    assert (
        workflow.get("concurrency"),
        jobs["prepare"].get("concurrency"),
        jobs["build"].get("concurrency"),
        jobs["migrate"]["concurrency"],
        "migration_controller.py run" in migration["run"],
        "migration_controller.py cleanup" in cleanup["run"],
        "always()" in cleanup["if"],
        "steps.migration.outcome == 'failure'" in cleanup["if"],
        "steps.migration.outcome == 'cancelled'" in cleanup["if"],
        all(
            '--state-file "$RUNNER_TEMP/migration-task-state.json"' in step["run"]
            and '--github-run-id "$GITHUB_RUN_ID"' in step["run"]
            and '--github-run-attempt "$GITHUB_RUN_ATTEMPT"' in step["run"]
            for step in (migration, cleanup)
        ),
        all(
            forbidden not in workflow_text
            for forbidden in (
                "AWS_ROLLOUT_ROLE_ARN",
                "update-service",
                "aws-app-images.yml",
                "decide_ecs_migration.py",
                "run_production_migration",
                "PUBLICATION_FREEZE",
            )
        ),
    ) == (
        None,
        None,
        None,
        {
            "group": "vector-production-change",
            "cancel-in-progress": "false",
            "queue": "max",
        },
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    )


def test_migration_base_is_the_only_template_without_per_attempt_state() -> None:
    ecs = (_ROOT / "infra/aws/ecs.tf").read_text(encoding="utf-8")
    base = ecs.split('resource "aws_ecs_task_definition" "migration_base" {', 1)[
        1
    ].split('\nresource "', 1)[0]
    assert (
        'resource "aws_ecs_task_definition" "migration" {' not in ecs,
        'family                   = "${var.name_prefix}-migration-base"' in base,
        'command    = ["python", "-m", "scripts.migration_runner"]' in base,
        all(
            forbidden not in base
            for forbidden in (
                "MIGRATION_PROTOCOL_VERSION",
                "MIGRATION_MODE",
                "MIGRATION_EXPECTED_START_REVISION",
                "MIGRATION_TARGET_REVISION",
                "MIGRATION_TREE_OID",
            )
        ),
        "aws_ecs_task_definition.migration_base.arn" not in ecs,
    ) == (True, True, True, True, True)
