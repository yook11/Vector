"""``EmbeddingFailureHandler`` の integration test (Stage 4 と同形)。

検証する性質:

- hold (stage 退避) は provider error の回復クラス (mode) から導出される:
  OPERATOR_ACTION_REQUIRED は即時 hold、CONDITION_BASED_RECOVERY は retry
  exhaustion (last_attempt) で hold、それ以外は hold しない。
- ``last_attempt`` flag で raise/return が分岐する (Recoverable / catch-all)。
- 失敗ごとに audit row が 1 行記録される (row の詳細 golden は
  ``test_audit_repository.py`` が所有)。
- audit Repository が raise しても task は落ちず ``embedding_failure_audit_dropped``
  にフォールバックし、secret prefix が log field から redact される。

marker は production と同じく ``to_embedding_error`` で provider error から構築する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import to_embedding_error
from app.analysis.embedding.failure_handling import EmbeddingFailureHandler
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
) -> AnalyzableArticleRecord:
    article = AnalyzableArticleRecord(
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


def _ready_for(
    article_id: int, *, analyzed_article_id: int = 1234
) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analyzed_article_id=analyzed_article_id,
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


def _input_rejected() -> AIProviderInputRejectedError:
    return AIProviderInputRejectedError(reason=GeminiContentRejectionReason.SAFETY)


_HOLD_CASES: list[tuple[AIProviderError, bool, str | None]] = [
    (AIProviderConfigurationError(), False, "ai_error_configuration"),
    (AIProviderConfigurationError(), True, "ai_error_configuration"),
    (_input_rejected(), False, None),
    (_input_rejected(), True, None),
    (AIProviderUsageLimitExhaustedError(), False, None),
    (AIProviderUsageLimitExhaustedError(), True, "ai_error_usage_limit_exhausted"),
    (AIProviderNetworkError(), True, None),
    (AIProviderRateLimitedError(), True, None),
    (AIProviderServiceUnavailableError(), True, None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_exc,last_attempt,expected_hold", _HOLD_CASES)
async def test_hold_reason_derived_from_provider_mode(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    provider_exc: AIProviderError,
    last_attempt: bool,
    expected_hold: str | None,
) -> None:
    """stage hold は marker 型ではなく provider error の回復クラスから決まる。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)
    exc = to_embedding_error(provider_exc)

    decision = await handler.handle(ready=ready, exc=exc, last_attempt=last_attempt)

    assert decision.stage_hold_reason == expected_hold


@pytest.mark.asyncio
async def test_recoverable_with_retry_budget_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Recoverable + retry 余地あり → taskiq retry に委ねる (reraise=True)。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = to_embedding_error(AIProviderNetworkError())
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert decision.reraise is True
    assert decision.stage_hold_reason is None


@pytest.mark.asyncio
async def test_recoverable_last_attempt_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Recoverable + retry 上限到達 → reraise=False。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = to_embedding_error(AIProviderNetworkError())
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=True)

    assert decision.reraise is False


@pytest.mark.asyncio
async def test_terminal_returns_false_without_reraise(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Terminal は retry 余地に関わらず reraise=False。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = to_embedding_error(AIProviderConfigurationError())
    decision = await handler.handle(ready=ready, exc=exc, last_attempt=False)

    assert decision.reraise is False


@pytest.mark.asyncio
async def test_terminal_writes_single_failure_audit_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Terminal で failure audit row が 1 行記録され原因軸が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = to_embedding_error(_input_rejected())
    await handler.handle(ready=ready, exc=exc, last_attempt=False)

    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_input_rejected"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "target_rejected"
    assert ev.payload["failure_reason"] == "safety"


@pytest.mark.asyncio
async def test_unexpected_with_retry_budget_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """catch-all + retry 余地あり → unknown audit + reraise=True。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    decision = await handler.handle(
        ready=ready, exc=ValueError("surprise"), last_attempt=False
    )

    assert decision.reraise is True
    assert decision.stage_hold_reason is None
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "unexpected_error"
    assert ev.retryability == "unknown"
    assert ev.payload["failure_kind"] == "unknown"


@pytest.mark.asyncio
async def test_unexpected_last_attempt_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """catch-all + retry 上限到達 → reraise=False。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)

    decision = await handler.handle(
        ready=ready, exc=ValueError("surprise"), last_attempt=True
    )

    assert decision.reraise is False
    assert decision.stage_hold_reason is None


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
    business_exc = to_embedding_error(AIProviderConfigurationError())

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
        # handler は落ちずに完走 (Terminal → reraise=False)
        decision = await handler.handle(
            ready=ready, exc=business_exc, last_attempt=False
        )

    assert decision.reraise is False
    assert decision.stage_hold_reason == "ai_error_configuration"
    drops = [e for e in cap if e.get("event") == "embedding_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["analyzed_article_id"] == ready.analyzed_article_id
    assert drop["business_error_class"].endswith(".EmbeddingTerminalError")
    assert drop["audit_error_class"].endswith(".RuntimeError")
    # business 側は code 固定値のみなので secret は入らない。
    assert "sk-live" not in drop["business_error_message"]
    # audit 側 (任意 RuntimeError) は redact_secrets で secret が落ちる。
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
