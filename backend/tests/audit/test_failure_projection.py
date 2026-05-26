"""失敗属性 projection の単体テスト。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InvalidRequestError,
    OperationalError,
)

from app.analysis.curation.errors import (
    CurationRecoverableError,
    CurationTerminalDropError,
)
from app.audit.categories import Layer1Category
from app.audit.domain.event import Stage
from app.audit.failure_projection import (
    FailureAction,
    FailureProjection,
    Retryability,
    legacy_category_for_projection,
    project_db_failure,
    project_failure,
    project_marker_failure,
)


def _stmt_error(cls: type[Exception]) -> Exception:
    """``StatementError`` 系 (``statement, params, orig``) を生成する。"""
    return cls("SELECT 1", {}, Exception("orig"))


def test_project_marker_failure_reads_stage_marker_classvars() -> None:
    exc = CurationTerminalDropError(code="ai_error_output_blocked")

    assert project_marker_failure(exc) == FailureProjection(
        failure_kind="terminal_drop",
        retryability=Retryability.NON_RETRYABLE,
        failure_action=FailureAction.DROP_ARTICLE,
        code="ai_error_output_blocked",
    )


def test_project_failure_prefers_marker_projection() -> None:
    exc = CurationRecoverableError(code="ai_error_network")

    assert project_failure(exc) == FailureProjection(
        failure_kind="recoverable",
        retryability=Retryability.RETRYABLE,
        failure_action=None,
        code="ai_error_network",
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            _stmt_error(OperationalError),
            FailureProjection(
                failure_kind="db_runtime",
                retryability=Retryability.RETRYABLE,
                failure_action=None,
                code="db_runtime_error",
            ),
        ),
        (
            _stmt_error(IntegrityError),
            FailureProjection(
                failure_kind="db_constraint",
                retryability=Retryability.NON_RETRYABLE,
                failure_action=None,
                code="db_constraint_error",
            ),
        ),
        (
            _stmt_error(DataError),
            FailureProjection(
                failure_kind="db_query_or_schema",
                retryability=Retryability.NON_RETRYABLE,
                failure_action=None,
                code="db_query_or_schema_error",
            ),
        ),
        (
            InvalidRequestError("boom"),
            FailureProjection(
                failure_kind="db_unknown",
                retryability=Retryability.UNKNOWN,
                failure_action=None,
                code="db_unknown_error",
            ),
        ),
    ],
)
def test_project_db_failure_maps_sqlalchemy_exceptions(
    exc: Exception, expected: FailureProjection
) -> None:
    assert project_db_failure(exc) == expected


def test_project_failure_returns_unknown_for_catch_all() -> None:
    assert project_failure(RuntimeError("boom")) == FailureProjection(
        failure_kind="unknown",
        retryability=Retryability.UNKNOWN,
        failure_action=None,
        code="unexpected_error",
    )


@pytest.mark.parametrize(
    ("stage", "projection", "expected"),
    [
        (
            Stage.CURATION,
            FailureProjection(
                failure_kind="terminal_drop",
                retryability=Retryability.NON_RETRYABLE,
                failure_action=FailureAction.DROP_ARTICLE,
                code="x",
            ),
            Layer1Category.NON_RETRYABLE_DROP_ARTICLE,
        ),
        (
            Stage.CURATION,
            FailureProjection("terminal_keep", Retryability.NON_RETRYABLE, None, "x"),
            Layer1Category.NON_RETRYABLE_KEEP_ARTICLE,
        ),
        (
            Stage.ASSESSMENT,
            FailureProjection("terminal_skip", Retryability.NON_RETRYABLE, None, "x"),
            Layer1Category.NON_RETRYABLE_KEEP_CURATION,
        ),
        (
            Stage.EMBEDDING,
            FailureProjection("terminal_skip", Retryability.NON_RETRYABLE, None, "x"),
            Layer1Category.NON_RETRYABLE_KEEP_CURATION,
        ),
        (
            Stage.BRIEFING,
            FailureProjection("configuration", Retryability.NON_RETRYABLE, None, "x"),
            Layer1Category.NON_RETRYABLE,
        ),
        (
            Stage.CURATION,
            FailureProjection("recoverable", Retryability.RETRYABLE, None, "x"),
            Layer1Category.RETRYABLE,
        ),
        (
            Stage.CURATION,
            FailureProjection("unknown", Retryability.UNKNOWN, None, "x"),
            Layer1Category.UNKNOWN,
        ),
    ],
)
def test_legacy_category_for_projection_preserves_existing_category_contract(
    stage: Stage, projection: FailureProjection, expected: Layer1Category
) -> None:
    category = legacy_category_for_projection(stage=stage, projection=projection)
    assert category == expected
