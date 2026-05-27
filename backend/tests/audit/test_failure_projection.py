"""失敗属性 projection の単体テスト。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InvalidRequestError,
    OperationalError,
)

from app.analysis.assessment.errors import AssessmentRecoverableError
from app.analysis.curation.errors import (
    CurationRecoverableError,
    CurationTerminalDropError,
)
from app.analysis.embedding.errors import EmbeddingRecoverableError
from app.audit.domain.event import Stage
from app.audit.failure_projection import (
    FailureAction,
    FailureProjection,
    Retryability,
    failure_payload_fields,
    project_db_failure,
    project_failure,
    project_marker_failure,
)
from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.collection.article_acquisition.errors import (
    AcquisitionExternalFetchRecoverableError,
    AcquisitionExternalFetchTerminalError,
    AcquisitionUnreadableResponseError,
    UnreadableResponseError,
)
from app.collection.article_completion.scrape_failure import (
    FetchFailed,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchGatewayError,
)
from app.insights.briefing.llm.errors import BriefingConfigurationError


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
        stage=Stage.CURATION,
    )


def test_project_failure_prefers_marker_projection() -> None:
    exc = CurationRecoverableError(code="ai_error_network")

    assert project_failure(exc) == FailureProjection(
        failure_kind="recoverable",
        retryability=Retryability.RETRYABLE,
        failure_action=None,
        code="ai_error_network",
        stage=Stage.CURATION,
    )


@pytest.mark.parametrize(
    ("exc", "expected_stage"),
    [
        (CurationRecoverableError(code="ai_error_network"), Stage.CURATION),
        (AssessmentRecoverableError(code="ai_error_network"), Stage.ASSESSMENT),
        (EmbeddingRecoverableError(code="ai_error_network"), Stage.EMBEDDING),
        (BriefingConfigurationError("missing key"), Stage.BRIEFING),
    ],
)
def test_project_marker_failure_reads_stage_from_parent_class(
    exc: BaseException, expected_stage: Stage
) -> None:
    projection = project_marker_failure(exc)

    assert projection is not None
    assert projection.stage is expected_stage


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
    ("exc", "expected"),
    [
        (
            AcquisitionExternalFetchRecoverableError(
                origin_error=FetchGatewayError(status_code=502)
            ),
            FailureProjection(
                failure_kind="external_fetch",
                retryability=Retryability.RETRYABLE,
                failure_action=None,
                code="fetch_gateway_failure",
                stage=Stage.ACQUISITION,
            ),
        ),
        (
            AcquisitionExternalFetchTerminalError(
                origin_error=FetchAccessDeniedError(status_code=403, reason="forbidden")
            ),
            FailureProjection(
                failure_kind="external_fetch",
                retryability=Retryability.NON_RETRYABLE,
                failure_action=None,
                code="fetch_access_denied",
                stage=Stage.ACQUISITION,
            ),
        ),
        (
            AcquisitionUnreadableResponseError(origin_error=UnreadableResponseError()),
            FailureProjection(
                failure_kind="unreadable_response",
                retryability=Retryability.NON_RETRYABLE,
                failure_action=None,
                code="read_unreadable_response",
                stage=Stage.ACQUISITION,
            ),
        ),
    ],
)
def test_source_acquisition_marker_projection_reads_classvars(
    exc: BaseException, expected: FailureProjection
) -> None:
    assert project_failure(exc) == expected


def test_completion_fetch_failed_projection_uses_scrape_decision() -> None:
    retryable = ArticleCompletionAuditRepository._projection_of_fetch_failed(
        FetchFailed(error=FetchGatewayError(status_code=502))
    )
    terminal = ArticleCompletionAuditRepository._projection_of_fetch_failed(
        FetchFailed(error=FetchAccessDeniedError(status_code=403, reason="forbidden"))
    )

    assert retryable == FailureProjection(
        failure_kind="external_fetch",
        retryability=Retryability.RETRYABLE,
        failure_action=None,
        code="fetch_gateway_failure",
    )
    assert terminal == FailureProjection(
        failure_kind="external_fetch",
        retryability=Retryability.NON_RETRYABLE,
        failure_action=None,
        code="fetch_access_denied",
    )


def test_completion_parse_crashed_projection_is_non_retryable() -> None:
    assert ArticleCompletionAuditRepository._projection_of_parse_crashed() == (
        FailureProjection(
            failure_kind="scrape_parse_crashed",
            retryability=Retryability.NON_RETRYABLE,
            failure_action=None,
            code="scrape_parse_crashed",
        )
    )


def test_completion_persist_crash_projection_uses_db_adapter() -> None:
    assert ArticleCompletionAuditRepository._projection_of_persist_crash(
        _stmt_error(OperationalError)
    ) == FailureProjection(
        failure_kind="db_runtime",
        retryability=Retryability.RETRYABLE,
        failure_action=None,
        code="persist_crashed",
    )


def test_completion_persist_crash_projection_returns_unknown_for_catch_all() -> None:
    assert ArticleCompletionAuditRepository._projection_of_persist_crash(
        RuntimeError("boom")
    ) == FailureProjection(
        failure_kind="persist_crashed",
        retryability=Retryability.UNKNOWN,
        failure_action=None,
        code="persist_crashed",
    )


def test_failure_payload_fields_serializes_action_value() -> None:
    projection = FailureProjection(
        failure_kind="terminal_drop",
        retryability=Retryability.NON_RETRYABLE,
        failure_action=FailureAction.DROP_ARTICLE,
        code="ai_error_output_blocked",
    )

    assert failure_payload_fields(projection) == {
        "failure_kind": "terminal_drop",
        "failure_action": "drop_article",
    }


def test_failure_payload_fields_keeps_missing_action_none() -> None:
    projection = FailureProjection(
        failure_kind="recoverable",
        retryability=Retryability.RETRYABLE,
        failure_action=None,
        code="ai_error_network",
    )

    assert failure_payload_fields(projection) == {
        "failure_kind": "recoverable",
        "failure_action": None,
    }
