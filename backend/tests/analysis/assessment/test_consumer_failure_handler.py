"""Consumerの失敗後処理を実DBと既存の通知出力で検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.ai_providers.errors import (
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.assessment.ai.deepseek import DeepSeekResponseDefect
from app.analysis.assessment.ai.gemini import GeminiResponseDefect
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.consumer_failure_classification import (
    classify_assessment_failure,
)
from app.analysis.assessment.consumer_failure_handling import (
    AssessmentConsumerFailureHandler,
)
from app.analysis.assessment.errors import (
    AssessmentCurationMissingError,
    AssessmentError,
    AssessmentResponseInvalidError,
    to_assessment_error,
)
from app.db.errors import DatabaseUnexpectedError
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent
from tests.cloudwatch.records import metric_records

_HANDLER = "app.analysis.assessment.consumer_failure_handling"


@pytest.fixture
async def article_id(db_session: AsyncSession, sample_source: NewsSource) -> int:
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url="https://example.com/consumer-failure",
        original_title="title",
        original_content="content",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    return article.id


async def _events(session: AsyncSession) -> list[PipelineEvent]:
    return list((await session.execute(select(PipelineEvent))).scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        AssessmentCurationMissingError(),
        *(
            AssessmentResponseInvalidError(defect)
            for defect in [
                *AssessmentResponseDefect,
                *DeepSeekResponseDefect,
                *GeminiResponseDefect,
            ]
        ),
        to_assessment_error(AIProviderNetworkError()),
        to_assessment_error(AIProviderRateLimitedError()),
        to_assessment_error(AIProviderUsageLimitExhaustedError()),
        to_assessment_error(AIProviderInsufficientBalanceError()),
        DatabaseUnexpectedError(),
        RuntimeError("Authorization: Bearer test-secret-do-not-record"),
    ],
)
async def test_records_classification_without_changing_original_error(
    db_session, session_factory, article_id, error, capsys
) -> None:
    """監査・失敗件数・必要な枯渇通知を記録し、元の例外をそのまま返せる。"""
    if isinstance(error, AssessmentError) and error.provider_error is not None:
        error.__cause__ = error.provider_error
    error.raw_response = "private-ai-response"
    error.input_text = "private-input"
    failure = classify_assessment_failure(error)
    cause = error.__cause__
    with pytest.raises(type(error)) as raised:
        try:
            raise error
        except Exception as caught:
            result = await AssessmentConsumerFailureHandler(session_factory).handle(
                failure=failure,
                exc=caught,
                curation_id=123,
                analyzable_article_id=article_id,
                provider="gemini",
            )
            assert result is None
            raise
    assert raised.value is error
    assert error.__cause__ is cause
    events = await _events(db_session)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "failed"
    assert event.outcome_code == failure.audit.code
    assert event.retryability == failure.audit.retryability.value
    assert event.payload["failure_kind"] == failure.audit.failure_kind
    assert event.payload["failure_reason"] == failure.audit.failure_reason
    assert event.payload["curation_id"] == 123
    assert event.article_id == article_id
    assert event.error_class == f"{type(error).__module__}.{type(error).__qualname__}"
    assert event.payload["error_chain"][0] == event.error_class
    if cause is not None:
        assert event.payload["error_chain"][1] == (
            f"{type(cause).__module__}.{type(cause).__qualname__}"
        )
    assert "test-secret-do-not-record" not in str(event.payload)
    assert event.payload["ai_raw_response"] is None
    assert event.payload["input_text"] is None
    output = capsys.readouterr().out
    outcomes = metric_records(output, "processing_outcome")
    assert len(outcomes) == 1
    assert outcomes[0]["result"] == "failed"
    notices = metric_records(output, "ai_provider_exhausted")
    if failure.provider_exhaustion is not None:
        assert len(notices) == 1
        assert notices[0]["kind"] == failure.audit.code
        assert notices[0]["provider"] == "gemini"
    else:
        assert notices == []


@pytest.mark.asyncio
async def test_audit_failure_does_not_prevent_notification(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys,
) -> None:
    """実DBの外部キー違反で監査が失敗しても枯渇通知を試みる。"""
    error = to_assessment_error(AIProviderUsageLimitExhaustedError())
    with capture_logs() as logs:
        await AssessmentConsumerFailureHandler(session_factory).handle(
            failure=classify_assessment_failure(error),
            exc=error,
            curation_id=123,
            analyzable_article_id=999_999,
            provider="gemini",
        )
    assert await _events(db_session) == []
    assert any(entry.get("operation") == "audit" for entry in logs)
    notices = metric_records(capsys.readouterr().out, "ai_provider_exhausted")
    assert len(notices) == 1
    assert notices[0]["provider"] == "gemini"


@pytest.mark.asyncio
async def test_notification_and_metric_failures_do_not_prevent_audit(
    db_session, session_factory, article_id
) -> None:
    """通知と計測の二次障害は本文をログに漏らさず、監査と元の失敗を維持する。"""
    error = to_assessment_error(AIProviderInsufficientBalanceError())
    with (
        capture_logs() as logs,
        patch(
            f"{_HANDLER}.record_assessment_processing_outcome",
            side_effect=RuntimeError("metric-secret"),
        ),
        patch(
            f"{_HANDLER}.record_ai_provider_exhausted",
            side_effect=RuntimeError("notification-secret"),
        ) as notify,
    ):
        await AssessmentConsumerFailureHandler(session_factory).handle(
            failure=classify_assessment_failure(error),
            exc=error,
            curation_id=123,
            analyzable_article_id=article_id,
            provider="gemini",
        )
    assert len(await _events(db_session)) == 1
    notify.assert_called_once_with(error.provider_error, provider="gemini")
    assert {entry["operation"] for entry in logs} == {
        "processing_metric",
        "notification",
    }
    assert "metric-secret" not in str(logs)
    assert "notification-secret" not in str(logs)


@pytest.mark.asyncio
async def test_secondary_reporting_failure_preserves_original_and_notification(
    db_session, session_factory, capsys
) -> None:
    """監査・drop計測・ログまで失敗しても元の例外を置き換えない。"""
    error = to_assessment_error(AIProviderUsageLimitExhaustedError())
    with (
        patch(
            f"{_HANDLER}.record_audit_dropped", side_effect=RuntimeError("drop failed")
        ),
        patch(f"{_HANDLER}.logger.warning", side_effect=RuntimeError("logger failed")),
        pytest.raises(type(error)) as raised,
    ):
        try:
            raise error
        except Exception:
            await AssessmentConsumerFailureHandler(session_factory).handle(
                failure=classify_assessment_failure(error),
                exc=error,
                curation_id=123,
                analyzable_article_id=999_999,
                provider="gemini",
            )
            raise
    assert raised.value is error
    assert await _events(db_session) == []
    assert len(metric_records(capsys.readouterr().out, "ai_provider_exhausted")) == 1


@pytest.mark.asyncio
async def test_audit_commit_failure_rolls_back_and_still_notifies(
    db_session, session_factory, article_id, capsys, capfire
):
    """失敗監査のcommitが失敗しても通知を試み、未確定の監査を残さない。"""

    class CommitFails(AsyncSession):
        async def commit(self):
            raise RuntimeError("commit-secret")

    factory = async_sessionmaker(
        session_factory.kw["bind"], class_=CommitFails, expire_on_commit=False
    )
    error = to_assessment_error(AIProviderUsageLimitExhaustedError())
    with capture_logs() as logs:
        await AssessmentConsumerFailureHandler(factory).handle(
            failure=classify_assessment_failure(error),
            exc=error,
            curation_id=123,
            analyzable_article_id=article_id,
            provider="deepseek",
        )
    assert await _events(db_session) == []
    assert "commit-secret" not in str(logs)
    output = capsys.readouterr().out
    assert len(metric_records(output, "ai_provider_exhausted")) == 1
    metric = next(
        m
        for m in capfire.get_collected_metrics()
        if m["name"] == "vector.audit.dropped"
    )
    assert len(metric["data"]["data_points"]) == 1
    point = metric["data"]["data_points"][0]
    assert point["value"] == 1
    assert point["attributes"] == {"stage": "assessment"}
