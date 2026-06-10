"""失敗属性 projection の単体テスト。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InvalidRequestError,
    OperationalError,
)

from app.analysis.ai_provider_errors import AIProviderOutputBlockedError
from app.analysis.assessment.errors import AssessmentRecoverableError
from app.analysis.curation.errors import (
    CurationRecoverableError,
    map_provider_to_curation,
)
from app.analysis.embedding.errors import EmbeddingRecoverableError
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
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
    AcquisitionReadError,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchGatewayError,
)
from app.insights.briefing.errors import BriefingConfigurationError


def _stmt_error(cls: type[Exception]) -> Exception:
    """``StatementError`` 系 (``statement, params, orig``) を生成する。"""
    return cls("SELECT 1", {}, Exception("orig"))


def test_project_marker_failure_reads_curation_instance_cause_axis() -> None:
    """curation も原因軸を instance 値で持つ (mapper 経由で mode 値 + reason 値)。

    retry / DROP 軸は marker classvar (NON_RETRYABLE / DROP_ARTICLE)、原因軸は
    provider error の ``FAILURE_MODE`` / ``reason`` 由来の instance 値。
    """
    raw = AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY)
    exc = map_provider_to_curation(raw)

    assert project_marker_failure(exc) == FailureProjection(
        failure_kind="target_rejected",
        retryability=Retryability.NON_RETRYABLE,
        failure_action=FailureAction.DROP_ARTICLE,
        code="ai_error_output_blocked",
        stage=Stage.CURATION,
        failure_reason="safety",
    )


def test_project_failure_prefers_marker_projection() -> None:
    exc = CurationRecoverableError(
        code="ai_error_network", failure_kind="attempt_scoped"
    )

    assert project_failure(exc) == FailureProjection(
        failure_kind="attempt_scoped",
        retryability=Retryability.RETRYABLE,
        failure_action=None,
        code="ai_error_network",
        stage=Stage.CURATION,
    )


def test_project_marker_failure_reads_instance_failure_kind_and_reason() -> None:
    """assessment / embedding は原因軸を instance 値で持つ (classvar より優先)。"""
    exc = AssessmentRecoverableError(
        code="ai_error_rate_limited",
        failure_kind="time_based_recovery",
        failure_reason="rate_limited",
    )

    assert project_marker_failure(exc) == FailureProjection(
        failure_kind="time_based_recovery",
        retryability=Retryability.RETRYABLE,
        failure_action=None,
        code="ai_error_rate_limited",
        stage=Stage.ASSESSMENT,
        failure_reason="rate_limited",
    )


def test_project_marker_failure_classvar_marker_has_no_failure_reason() -> None:
    """classvar 宣言 marker (briefing / completion / acquisition) は failure_reason
    を持たない (None)。原因軸を instance 値で持つのは assessment / embedding /
    curation のみで、classvar fallback 経路は reason を焼かない。
    """
    projection = project_marker_failure(BriefingConfigurationError("missing key"))

    assert projection is not None
    assert projection.failure_reason is None


@pytest.mark.parametrize(
    ("exc", "expected_stage"),
    [
        (
            CurationRecoverableError(
                code="ai_error_network", failure_kind="attempt_scoped"
            ),
            Stage.CURATION,
        ),
        (
            AssessmentRecoverableError(
                code="ai_error_network", failure_kind="attempt_scoped"
            ),
            Stage.ASSESSMENT,
        ),
        (
            EmbeddingRecoverableError(
                code="ai_error_network", failure_kind="attempt_scoped"
            ),
            Stage.EMBEDDING,
        ),
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
            AcquisitionReadError(origin=FetchGatewayError(status_code=502)),
            FailureProjection(
                failure_kind="external_fetch",
                retryability=Retryability.RETRYABLE,
                failure_action=None,
                code="fetch_gateway_failure",
                stage=Stage.ACQUISITION,
            ),
        ),
        (
            AcquisitionReadError(
                origin=FetchAccessDeniedError(status_code=403, reason="forbidden")
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
            AcquisitionReadError(
                origin=UnreadableResponseError(
                    reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
                    response_format="json",
                    field="items",
                )
            ),
            FailureProjection(
                failure_kind="unreadable_response",
                retryability=Retryability.NON_RETRYABLE,
                failure_action=None,
                code="read_unexpected_field_shape",
                stage=Stage.ACQUISITION,
            ),
        ),
    ],
)
def test_source_acquisition_marker_projection_reads_marker_attrs(
    exc: BaseException, expected: FailureProjection
) -> None:
    """統合 marker は instance ``RETRYABILITY`` を origin から導いて projection する。

    gateway (retryable) と access_denied (terminal) を同一クラスで構築し、片方は
    ``RETRYABLE`` 片方は ``NON_RETRYABLE`` に投影される (per-instance 導出の witness)。
    """
    assert project_failure(exc) == expected


def test_completion_fetch_failed_projection_uses_scrape_decision() -> None:
    retryable = ArticleCompletionAuditRepository._projection_of_fetch_failed(
        FetchGatewayError(status_code=502)
    )
    terminal = ArticleCompletionAuditRepository._projection_of_fetch_failed(
        FetchAccessDeniedError(status_code=403, reason="forbidden")
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
        failure_kind="target_rejected",
        retryability=Retryability.NON_RETRYABLE,
        failure_action=FailureAction.DROP_ARTICLE,
        code="ai_error_output_blocked",
    )

    assert failure_payload_fields(projection) == {
        "failure_kind": "target_rejected",
        "failure_action": "drop_article",
    }


def test_failure_payload_fields_keeps_missing_action_none() -> None:
    projection = FailureProjection(
        failure_kind="attempt_scoped",
        retryability=Retryability.RETRYABLE,
        failure_action=None,
        code="ai_error_network",
    )

    assert failure_payload_fields(projection) == {
        "failure_kind": "attempt_scoped",
        "failure_action": None,
    }
