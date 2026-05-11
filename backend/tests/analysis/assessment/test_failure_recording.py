"""``record_assessment_failure`` の単体テスト (PR6)。

extraction pattern (``record_extraction_failure``) と 1:1 同型。検証する性質:

- 正常系: 別 session で 1 行 INSERT (業務 tx と独立)
- ``attempt`` が ``pipeline_events.attempt`` に焼かれる
- ``payload.extraction_id`` が ``ready.extraction_id`` と一致
- Layer 1 marker → category 自動導出 (Recoverable / TerminalSkip)
- session_factory が常に raise する場合 → ``assessment_failure_audit_dropped``
  log fallback、business exception を再 raise しない
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.analysis.assessment.failure_recording import record_assessment_failure
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


async def _make_extraction(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> ArticleExtraction:
    article = Article(
        source_id=sample_source.id,
        source_url="https://e.com/a",  # type: ignore[arg-type]
        original_title="t",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="title",
        summary="summary",
        ai_model="gemini-2.5-pro",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready(
    extraction: ArticleExtraction, *, source_name: str | None = None
) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=extraction.summary,
        article_id=extraction.article_id,
        source_name=source_name,
    )


# ---------------------------------------------------------------------------
# 正常系: 別 session で 1 行 INSERT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_failure_in_separate_session(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """別 session で audit が 1 行 INSERT される (業務 tx と独立)。"""
    extraction = await _make_extraction(db_session, sample_source)
    exc = AssessmentRecoverableError("transient", code="ai_error_network")

    await record_assessment_failure(
        session_factory,
        ready=_ready(extraction),
        exc=exc,
        attempt=2,
    )

    rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "failed"
    assert row.attempt == 2
    assert row.payload["extraction_id"] == extraction.id


@pytest.mark.asyncio
async def test_attempt_is_recorded(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """``attempt=3`` を渡すと ``pipeline_events.attempt == 3``。"""
    extraction = await _make_extraction(db_session, sample_source)
    exc = AssessmentTerminalSkipError("terminal", code="ai_error_configuration")

    await record_assessment_failure(
        session_factory,
        ready=_ready(extraction),
        exc=exc,
        attempt=3,
    )

    row = (await db_session.execute(select(PipelineEvent))).scalar_one()
    assert row.attempt == 3


# ---------------------------------------------------------------------------
# Layer 1 marker → category 自動導出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_marker_category_dispatch_recoverable(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """``AssessmentRecoverableError`` → ``category=retryable`` で焼かれる。"""
    extraction = await _make_extraction(db_session, sample_source)
    exc = AssessmentRecoverableError("transient", code="ai_error_network")

    await record_assessment_failure(
        session_factory,
        ready=_ready(extraction),
        exc=exc,
        attempt=1,
    )

    row = (await db_session.execute(select(PipelineEvent))).scalar_one()
    assert row.category == "retryable"
    assert row.code == "ai_error_network"


@pytest.mark.asyncio
async def test_marker_category_dispatch_terminal_skip(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """``AssessmentTerminalSkipError`` → ``non_retryable_keep_extraction``。"""
    extraction = await _make_extraction(db_session, sample_source)
    exc = AssessmentTerminalSkipError("terminal", code="assessment_category_missing")

    await record_assessment_failure(
        session_factory,
        ready=_ready(extraction),
        exc=exc,
        attempt=1,
    )

    row = (await db_session.execute(select(PipelineEvent))).scalar_one()
    assert row.category == "non_retryable_keep_extraction"
    assert row.code == "assessment_category_missing"


# ---------------------------------------------------------------------------
# audit INSERT 失敗 → log fallback (business exception は再 raise しない)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_insert_failure_logs_and_swallows() -> None:
    """``session_factory`` が常に raise する場合、log fallback で観測可能。"""

    class _BoomFactory:
        def __call__(self) -> Any:
            raise RuntimeError("db down")

    ready = ReadyForAssessment(
        extraction_id=42,
        translated_title="t",
        summary="s",
        article_id=7,
        source_name=None,
    )
    business_exc = AssessmentRecoverableError("net timeout", code="ai_error_network")

    with capture_logs() as cap:
        # business exception を再 raise しないことも同時に検証
        await record_assessment_failure(
            _BoomFactory(),  # type: ignore[arg-type]
            ready=ready,
            exc=business_exc,
            attempt=3,
        )

    drops = [e for e in cap if e.get("event") == "assessment_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["extraction_id"] == 42
    assert drop["attempt"] == 3
    assert drop["business_error_class"].endswith(".AssessmentRecoverableError")
    assert drop["business_error_message"] == "net timeout"
    assert drop["audit_error_class"].endswith(".RuntimeError")
