"""``AssessmentFailureHandler`` の integration test。

Stage 4 は内容起因 DELETE 経路を持たないため、検証する性質は:

- TerminalSkip / Recoverable / catch-all の各 marker で audit row が正しい
  ``category`` / ``code`` / ``outcome_code`` で記録される
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

from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
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
    extraction = await _make_extraction(db_session, article)
    # rollback 後の expired-attr lazy reload を避けるため事前に値を取り出す
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = AssessmentTerminalSkipError("bad config", code="ai_error_configuration")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=1, last_attempt=False)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
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
    """Recoverable + retry 余地あり → ``category='retryable'`` audit + reraise=True。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = AssessmentRecoverableError("network", code="ai_error_network")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=1, last_attempt=False)

    assert reraise is True
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
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
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = AssessmentRecoverableError("network", code="ai_error_network")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=3, last_attempt=True)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
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
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = ValueError("surprise")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=1, last_attempt=False)

    assert reraise is True
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
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
    extraction = await _make_extraction(db_session, article)
    article_id = article.id
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    exc = ValueError("surprise")
    reraise = await handler.handle(ready=ready, exc=exc, attempt=3, last_attempt=True)

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_assessment_events(db_session, article_id)
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
    ``assessment_failure_audit_dropped`` log にフォールバックする。
    business / audit exception message に混入した secret prefix が log field
    から redact されることも検証する (red-team chain γ-2 対称化)。"""
    article = await _make_article(db_session, sample_source)
    extraction = await _make_extraction(db_session, article)
    ready = _ready_from(extraction)
    handler = AssessmentFailureHandler(session_factory)

    business_exc = AssessmentTerminalSkipError(
        "config Authorization: Bearer sk-live-BUSINESSSECRETabc",
        code="ai_error_configuration",
    )

    with (
        patch(
            "app.analysis.assessment.failure_handling.AssessmentAuditRepository"
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
    drops = [e for e in cap if e.get("event") == "assessment_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["curation_id"] == extraction.id
    assert drop["attempt"] == 1
    assert drop["business_error_class"].endswith(".AssessmentTerminalSkipError")
    assert drop["audit_error_class"].endswith(".RuntimeError")
    # red-team chain γ-2: business / audit 両方の secret が redact される
    assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
