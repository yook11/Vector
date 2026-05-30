"""``EmbeddingFailureHandler`` の integration test。

embedding は内容起因 DELETE 経路を持たないため、検証する性質は:

- Terminal / Recoverable / catch-all の各 marker で audit row が正しい
  ``outcome_code`` / ``retryability`` / payload failure attrs で記録される
- ``last_attempt`` flag で raise/return が分岐する (Recoverable / catch-all)
- audit Repository が raise しても task は落ちず ``embedding_failure_audit_dropped``
  構造ログにフォールバックする (business / audit exception の secret prefix
  が log field から除去される)
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
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingTerminalStageBlockedError,
    EmbeddingTerminalTargetRejectedError,
)
from app.analysis.embedding.failure_handling import EmbeddingFailureHandler
from app.models.article import Article
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


def _ready_for(article_id: int, *, analysis_id: int = 1234) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=analysis_id,
        text_for_embedding="分析タイトル\n分析要約",
        article_id=article_id,
    )


async def _fetch_embedding_events(
    db_session: AsyncSession, article_id: int
) -> list[PipelineEvent]:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent)
                .where(PipelineEvent.article_id == article_id)
                .where(PipelineEvent.stage == "embedding")
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
    """StageBlocked → non-retryable audit + embedding hold + ``reraise=False``。"""
    article = await _make_article(db_session, sample_source)
    # rollback 後の expired-attr lazy reload を避けるため事前に値を取り出す
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = EmbeddingTerminalStageBlockedError(code="ai_error_configuration")
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert decision.reraise is False
    assert decision.stage_hold_reason == "ai_error_configuration"
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
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
    """TargetRejected は対象 assessment 固有の失敗なので hold を立てない。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = EmbeddingTerminalTargetRejectedError(code="ai_error_input_rejected")
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert decision.reraise is False
    assert decision.stage_hold_reason is None
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "ai_error_input_rejected"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "terminal_target_rejected"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_terminal_stage_blocked_returns_hold_reason_without_redis(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """handler は Redis に触らず、hold reason だけを decision に乗せる。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = EmbeddingTerminalStageBlockedError(code="ai_error_configuration")

    decision = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert decision.reraise is False
    assert decision.stage_hold_reason == "ai_error_configuration"
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
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
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = EmbeddingRecoverableError(code="ai_error_network")
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert decision.reraise is True
    assert decision.stage_hold_reason is None
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
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
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = EmbeddingRecoverableError(code="ai_error_network")
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert decision.reraise is False
    assert decision.stage_hold_reason is None
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
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
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)
    provider_exc = AIProviderUsageLimitExhaustedError()
    exc = EmbeddingRecoverableError(
        code=provider_exc.CODE,
        provider_error=provider_exc,
    )

    decision = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert decision.reraise is True
    assert decision.stage_hold_reason is None


@pytest.mark.asyncio
async def test_usage_limit_recoverable_sets_hold_on_last_attempt(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """UsageLimitExhausted は recoverable のまま retry exhaustion で hold する。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)
    provider_exc = AIProviderUsageLimitExhaustedError()
    exc = EmbeddingRecoverableError(
        code=provider_exc.CODE,
        provider_error=provider_exc,
    )

    decision = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert decision.reraise is False
    assert decision.stage_hold_reason == provider_exc.CODE
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].outcome_code == provider_exc.CODE
    assert events[0].retryability == "retryable"
    assert events[0].payload["failure_kind"] == "recoverable"


@pytest.mark.asyncio
async def test_rate_limited_recoverable_last_attempt_does_not_set_hold(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """RateLimited は短期 throttle として recoverable exhaustion でも hold しない。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)
    provider_exc = AIProviderRateLimitedError()
    exc = EmbeddingRecoverableError(
        code=provider_exc.CODE,
        provider_error=provider_exc,
    )

    decision = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert decision.reraise is False
    assert decision.stage_hold_reason is None


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
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = ValueError("surprise")
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert decision.reraise is True
    assert decision.stage_hold_reason is None
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
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
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = ValueError("surprise")
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert decision.reraise is False
    assert decision.stage_hold_reason is None
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].retryability == "unknown"
    assert events[0].payload["failure_kind"] == "unknown"


# ---------------------------------------------------------------------------
# audit DB 落ち時の log fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_failure_falls_back_to_log_with_secrets_redacted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """audit Repository が raise しても handler は完走し
    ``embedding_failure_audit_dropped`` log にフォールバックする。
    business / audit exception message に混入した secret prefix が log field
    から redact されることも検証する。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)

    # business 側の例外は __str__ が code 固定値のみ。
    business_exc = EmbeddingTerminalStageBlockedError(code="ai_error_configuration")

    with (
        patch(
            "app.analysis.embedding.failure_handling.EmbeddingAuditRepository"
        ) as mock_audit_cls,
        capture_logs() as cap,
    ):
        mock_audit_cls.return_value.append_failure = AsyncMock(
            side_effect=RuntimeError(
                "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
            )
        )
        # handler は落ちずに完走 (StageBlocked → reraise=False)
        decision = await handler.handle(
            ready=ready, exc=business_exc, last_attempt=False
        )

    assert decision.reraise is False
    assert decision.stage_hold_reason == "ai_error_configuration"
    drops = [e for e in cap if e.get("event") == "embedding_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["analysis_id"] == ready.analysis_id
    assert drop["business_error_class"].endswith(".EmbeddingTerminalStageBlockedError")
    assert drop["audit_error_class"].endswith(".RuntimeError")
    # business 側は code 固定値のみなので secret は入らない。
    assert "sk-live" not in drop["business_error_message"]
    # audit 側 (任意 RuntimeError) は redact_secrets で secret が落ちる。
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
