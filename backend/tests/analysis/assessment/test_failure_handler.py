"""``AssessmentFailureHandler`` の integration test。

Stage 4 は内容起因 DELETE 経路を持たないため、検証する性質は:

- Terminal / Recoverable / catch-all の各 marker で audit row が正しい
  ``outcome_code`` / ``retryability`` / payload failure attrs で記録される
- ``last_attempt`` flag で raise/return が分岐する (Recoverable / catch-all)
- audit Repository が raise しても task は落ちず ``assessment_failure_audit_dropped``
  構造ログにフォールバックする (business / audit exception の secret prefix
  が log field から除去される、red-team chain γ-2 対称化)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import (
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentCategoryMissingError,
    AssessmentRecoverableError,
    AssessmentTerminalStageBlockedError,
    AssessmentTerminalTargetRejectedError,
)
from app.analysis.assessment.failure_handling import AssessmentFailureHandler
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="t",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


async def _make_extraction(
    db_session: AsyncSession,
    article: Article,
) -> ArticleCuration:
    extraction = ArticleCuration(
        article_id=article.id,
        translated_title="title",
        summary="summary text",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready_from(extraction: ArticleCuration) -> ReadyForAssessment:
    return ReadyForAssessment(
        curation_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=extraction.summary,
        article_id=extraction.article_id,
        source_name="Test Source",
    )


async def _fetch_assessment_events(
    db_session: AsyncSession, article_id: int
) -> list[PipelineEvent]:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent)
                .where(PipelineEvent.article_id == article_id)
                .where(PipelineEvent.stage == "assessment")
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Terminal 経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_stage_blocked_writes_audit_sets_hold_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """StageBlocked → non-retryable audit + assessment hold + ``reraise=False``。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    # rollback 後の expired-attr lazy reload を避けるため事前に値を取り出す
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = AssessmentTerminalStageBlockedError(code="ai_error_configuration")
    with patch(
        "app.analysis.assessment.failure_handling.set_assessment_hold",
        new=AsyncMock(),
    ) as set_hold:
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert reraise is False
    set_hold.assert_awaited_once()
    assert set_hold.await_args.kwargs["reason"] == "ai_error_configuration"
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_configuration"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_stage_blocked"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_terminal_target_rejected_writes_audit_without_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """TargetRejected は対象 curation 固有の失敗なので hold を立てない。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = AssessmentTerminalTargetRejectedError(code="ai_error_input_rejected")
    with patch(
        "app.analysis.assessment.failure_handling.set_assessment_hold",
        new=AsyncMock(),
    ) as set_hold:
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert reraise is False
    set_hold.assert_not_called()
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "ai_error_input_rejected"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_target_rejected"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_category_missing_writes_audit_without_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """CategoryMissing は分類未解決として audit のみ焼き、hold は立てない。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = AssessmentCategoryMissingError()
    with patch(
        "app.analysis.assessment.failure_handling.set_assessment_hold",
        new=AsyncMock(),
    ) as set_hold:
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert reraise is False
    set_hold.assert_not_called()
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "assessment_category_missing"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_classification_unresolved"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_terminal_stage_blocked_redis_failure_still_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """hold set の Redis 障害は helper が呑み、handler は完走する。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RuntimeError("redis down")
    exc = AssessmentTerminalStageBlockedError(code="ai_error_configuration")

    with patch(
        "app.analysis.assessment.failure_handling.get_redis",
        return_value=fake_redis,
    ):
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].payload["failure_kind"] == "terminal_stage_blocked"


# ---------------------------------------------------------------------------
# Recoverable 経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recoverable_with_retry_budget_writes_audit_and_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Recoverable + retry 余地あり → retryable audit + reraise=True。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = AssessmentRecoverableError(code="ai_error_network")
    reraise = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert reraise is True
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_network"
    assert ev.retryability == "retryable"
    assert ev.payload["failure_kind"] == "recoverable"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_recoverable_last_attempt_writes_audit_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Recoverable + retry 上限到達 → audit + ``reraise=False``。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = AssessmentRecoverableError(code="ai_error_network")
    with patch(
        "app.analysis.assessment.failure_handling.set_assessment_hold",
        new=AsyncMock(),
    ) as set_hold:
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert reraise is False
    set_hold.assert_not_called()
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].retryability == "retryable"
    assert events[0].payload["failure_kind"] == "recoverable"


@pytest.mark.asyncio
async def test_usage_limit_recoverable_with_retry_budget_does_not_set_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """UsageLimitExhausted でも retry 余地があれば taskiq retry に任せる。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)
    provider_exc = AIProviderUsageLimitExhaustedError()
    exc = AssessmentRecoverableError(
        code=provider_exc.CODE,
        provider_error=provider_exc,
    )

    with patch(
        "app.analysis.assessment.failure_handling.set_assessment_hold",
        new=AsyncMock(),
    ) as set_hold:
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert reraise is True
    set_hold.assert_not_called()


@pytest.mark.asyncio
async def test_usage_limit_recoverable_sets_hold_on_last_attempt(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """UsageLimitExhausted は recoverable のまま retry exhaustion で hold する。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)
    provider_exc = AIProviderUsageLimitExhaustedError()
    exc = AssessmentRecoverableError(
        code=provider_exc.CODE,
        provider_error=provider_exc,
    )

    with patch(
        "app.analysis.assessment.failure_handling.set_assessment_hold",
        new=AsyncMock(),
    ) as set_hold:
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert reraise is False
    set_hold.assert_awaited_once()
    assert set_hold.await_args.kwargs["reason"] == provider_exc.CODE
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].outcome_code == provider_exc.CODE
    assert events[0].retryability == "retryable"
    assert events[0].payload["failure_kind"] == "recoverable"


@pytest.mark.asyncio
async def test_usage_limit_hold_redis_failure_still_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """UsageLimitExhausted hold の Redis 障害は helper が呑み、handler は完走する。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RuntimeError("redis down")
    provider_exc = AIProviderUsageLimitExhaustedError()
    exc = AssessmentRecoverableError(
        code=provider_exc.CODE,
        provider_error=provider_exc,
    )

    with patch(
        "app.analysis.assessment.failure_handling.get_redis",
        return_value=fake_redis,
    ):
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].outcome_code == provider_exc.CODE
    assert events[0].payload["failure_kind"] == "recoverable"


@pytest.mark.asyncio
async def test_rate_limited_recoverable_last_attempt_does_not_set_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """RateLimited は短期 throttle として recoverable exhaustion でも hold しない。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)
    provider_exc = AIProviderRateLimitedError()
    exc = AssessmentRecoverableError(
        code=provider_exc.CODE,
        provider_error=provider_exc,
    )

    with patch(
        "app.analysis.assessment.failure_handling.set_assessment_hold",
        new=AsyncMock(),
    ) as set_hold:
        reraise = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert reraise is False
    set_hold.assert_not_called()


# ---------------------------------------------------------------------------
# catch-all 経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_with_retry_budget_writes_audit_and_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """catch-all + retry 余地あり → unknown audit + ``reraise=True``。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = ValueError("surprise")
    reraise = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert reraise is True
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "unexpected_error"
    assert ev.retryability == "unknown"
    assert ev.payload["failure_kind"] == "unknown"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_unexpected_last_attempt_writes_audit_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """catch-all + retry 上限到達 → audit + ``reraise=False``。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = ValueError("surprise")
    reraise = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].retryability == "unknown"
    assert events[0].payload["failure_kind"] == "unknown"


# ---------------------------------------------------------------------------
# audit DB 落ち時の log fallback (red-team chain γ-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_failure_falls_back_to_log_with_secrets_redacted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """audit Repository が raise しても handler は完走し
    ``assessment_failure_audit_dropped`` log にフォールバックする。
    business / audit exception message に混入した secret prefix が log field
    から redact されることも検証する (red-team chain γ-2 対称化)。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    # Phase 4: AssessmentTerminalStageBlockedError は kwargs-only constructor。
    # business 側の secret 混入経路は Phase 4 で構造的に塞がれている
    # (__str__ は code 固定値のみ、SAFE_ATTRS=("code",))。
    business_exc = AssessmentTerminalStageBlockedError(code="ai_error_configuration")

    with (
        patch(
            "app.analysis.assessment.failure_handling.AssessmentAuditRepository"
        ) as mock_audit_cls,
        patch(
            "app.analysis.assessment.failure_handling.set_assessment_hold",
            new=AsyncMock(),
        ),
        capture_logs() as cap,
    ):
        mock_audit_cls.return_value.append_failure = AsyncMock(
            side_effect=RuntimeError(
                "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
            )
        )
        # handler は落ちずに完走 (StageBlocked → reraise=False)
        reraise = await handler.handle(
            ready=ready, exc=business_exc, last_attempt=False
        )

    assert reraise is False
    drops = [e for e in cap if e.get("event") == "assessment_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["curation_id"] == extraction.id
    assert drop["business_error_class"].endswith(".AssessmentTerminalStageBlockedError")
    assert drop["audit_error_class"].endswith(".RuntimeError")
    # business: Phase 4 で __str__ が SAFE_ATTRS のみになり secret は原理上不在。
    assert "sk-live" not in drop["business_error_message"]
    # red-team chain γ-2: audit 側 (任意 RuntimeError) は redact_secrets で
    # secret 落ち。
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
