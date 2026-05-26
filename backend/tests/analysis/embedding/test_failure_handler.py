"""``EmbeddingFailureHandler`` の integration test。

Stage 5 も内容起因 DELETE 経路を持たないため、検証する性質は:

- TerminalSkip / Recoverable / catch-all の各 marker で audit row が正しい
  ``category`` / ``code`` / ``outcome_code`` で記録される
- ``last_attempt`` flag で raise/return が分岐する (Recoverable / catch-all)
- audit Repository が raise しても task は落ちず ``embedding_failure_audit_dropped``
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

from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingTerminalSkipError,
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
# TerminalSkip 経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_skip_writes_audit_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """TerminalSkip → ``category='non_retryable_keep_curation'`` / ``code=exc.code``
    の audit + ``reraise=False``。"""
    article = await _make_article(db_session, sample_source)
    # rollback 後の expired-attr lazy reload を避けるため事前に値を取り出す
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = EmbeddingTerminalSkipError(code="ai_error_configuration")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=1, last_attempt=False)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.category == "non_retryable_keep_curation"
    assert ev.code == "ai_error_configuration"
    assert ev.outcome_code == "ai_error_configuration"


# ---------------------------------------------------------------------------
# Recoverable 経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recoverable_with_retry_budget_writes_audit_and_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Recoverable + retry 余地あり → category='retryable' audit + reraise=True。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = EmbeddingRecoverableError(code="ai_error_network")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=1, last_attempt=False)

    assert reraise is True
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.category == "retryable"
    assert ev.code == "ai_error_network"


@pytest.mark.asyncio
async def test_recoverable_last_attempt_writes_audit_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Recoverable + 最終 attempt → audit + ``reraise=False``。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = EmbeddingRecoverableError(code="ai_error_network")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=3, last_attempt=True)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].category == "retryable"


# ---------------------------------------------------------------------------
# catch-all 経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_with_retry_budget_writes_audit_and_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """catch-all + retry 余地あり → ``category='unknown'`` / ``code='unexpected_error'``
    audit + ``reraise=True``。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = ValueError("surprise")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=1, last_attempt=False)

    assert reraise is True
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.category == "unknown"
    assert ev.code == "unexpected_error"


@pytest.mark.asyncio
async def test_unexpected_last_attempt_writes_audit_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """catch-all + 最終 attempt → audit + ``reraise=False``。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for(article_id)
    handler = EmbeddingFailureHandler(session_factory)

    exc = ValueError("surprise")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=3, last_attempt=True)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    assert events[0].category == "unknown"


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
    ``embedding_failure_audit_dropped`` log にフォールバックする。
    business / audit exception message に混入した secret prefix が log field
    から redact されることも検証する (red-team chain γ-2 対称化)。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for(article.id)
    handler = EmbeddingFailureHandler(session_factory)

    # Phase 4: EmbeddingTerminalSkipError は kwargs-only constructor。
    # business 側の secret 混入経路は Phase 4 で構造的に塞がれている
    # (__str__ は code 固定値のみ、SAFE_ATTRS=("code",))。
    business_exc = EmbeddingTerminalSkipError(code="ai_error_configuration")

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
        # handler は落ちずに完走 (TerminalSkip → reraise=False)
        reraise = await handler.handle(
            ready=ready, exc=business_exc, attempt=1, last_attempt=False
        )

    assert reraise is False
    drops = [e for e in cap if e.get("event") == "embedding_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["analysis_id"] == ready.analysis_id
    assert drop["attempt"] == 1
    assert drop["business_error_class"].endswith(".EmbeddingTerminalSkipError")
    assert drop["audit_error_class"].endswith(".RuntimeError")
    # business: Phase 4 で __str__ が SAFE_ATTRS のみになり、secret が原理上
    # 混入しない (= business_error_message に code 文字列のみ残る)。
    assert "sk-live" not in drop["business_error_message"]
    # red-team chain γ-2: audit 側 (任意 RuntimeError) は redact_secrets で
    # secret が落ちることを検証。
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
