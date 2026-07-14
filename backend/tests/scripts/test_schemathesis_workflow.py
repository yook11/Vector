"""Schemathesis workflowの公開前fail-closed契約テスト。"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "schemathesis-nightly.yml"
_SCHEMATHESIS_USER_ID = "00000000-0000-0000-0000-000000000001"


def _workflow_steps() -> list[str]:
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    starts = [
        match.start()
        for match in re.finditer(r"(?m)^      - (?=name:|uses:)", workflow)
    ]
    return [
        workflow[start : starts[index + 1] if index + 1 < len(starts) else None]
        for index, start in enumerate(starts)
    ]


def _step_containing(fragment: str) -> str:
    matches = [step for step in _workflow_steps() if fragment in step]
    assert len(matches) == 1, f"expected one workflow step containing {fragment!r}"
    return matches[0]


def _if_expression(step: str) -> str:
    match = re.search(r"(?m)^        if:\s*(.+?)\s*$", step)
    assert match is not None, "workflow step must declare an if expression"
    return match.group(1)


def test_redaction_runs_after_schemathesis_even_when_fuzzing_fails() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    schemathesis_step = _step_containing("name: Run Schemathesis (blocking)")
    redact_step = _step_containing("id: redact_artifacts")

    assert workflow.index(schemathesis_step) < workflow.index(redact_step)
    assert "always()" in _if_expression(redact_step)
    assert "schemathesis-report" in redact_step
    assert "${RUNNER_TEMP}/backend.log" in redact_step
    assert redact_step.index("backend.pid") < redact_step.index("kill -TERM")
    assert redact_step.index("kill -TERM") < redact_step.index(
        "python -m scripts.redact_schemathesis_artifacts"
    )
    assert "uvicorn app.main:app" in redact_step
    assert "backend did not stop before artifact redaction" in redact_step


def test_artifact_uploads_require_successful_redaction() -> None:
    result_upload = _step_containing("name: schemathesis-results")
    log_upload = _step_containing("name: schemathesis-backend-log")

    assert (
        " ".join(_if_expression(result_upload).split()),
        " ".join(_if_expression(log_upload).split()),
    ) == (
        "${{ always() && steps.redact_artifacts.outcome == 'success' }}",
        "${{ failure() && steps.redact_artifacts.outcome == 'success' }}",
    )


def test_ci_user_seed_matches_schemathesis_jwt_subject_without_account() -> None:
    seed_step = _step_containing('INSERT INTO auth."user"')
    jwt_step = _step_containing("name: Mint admin JWT for Schemathesis")
    insert = re.search(
        r'INSERT\s+INTO\s+auth\."user"\s*\((?P<columns>[^)]*)\)'
        r"\s*VALUES\s*\((?P<values>[^)]*)\)",
        seed_step,
        flags=re.IGNORECASE | re.DOTALL,
    )

    assert insert is not None
    columns = {
        column.strip().strip('"') for column in insert.group("columns").split(",")
    }
    assert columns == {
        "id",
        "name",
        "email",
        "emailVerified",
        "createdAt",
        "updatedAt",
        "role",
    }
    assert _SCHEMATHESIS_USER_ID in insert.group("values")
    assert "example.invalid" in insert.group("values")
    assert _SCHEMATHESIS_USER_ID in jwt_step
    assert "password" not in insert.group("columns").lower()
    assert 'INSERT INTO auth."account"' not in seed_step
