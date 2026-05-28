"""Ready build failed の outcome_code 分類テスト。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.audit.ready_build import project_ready_build_failure


class _PositiveModel(BaseModel):
    value: int = Field(gt=0)


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        _PositiveModel(value=0)
    return exc_info.value


@pytest.mark.parametrize(
    ("exc", "expected_code", "expected_kind"),
    [
        (
            SQLAlchemyError("db exploded"),
            "curation_ready_build_failed_db_error",
            "db_error",
        ),
        (
            _validation_error(),
            "curation_ready_build_failed_contract_invalid",
            "contract_invalid",
        ),
        (
            RuntimeError("boom"),
            "curation_ready_build_failed_unexpected_error",
            "unexpected_error",
        ),
    ],
)
def test_project_ready_build_failure_uses_three_way_classification(
    exc: Exception,
    expected_code: str,
    expected_kind: str,
) -> None:
    projection = project_ready_build_failure(stage_prefix="curation", exc=exc)

    assert projection.outcome_code == expected_code
    assert projection.failure_kind == expected_kind
