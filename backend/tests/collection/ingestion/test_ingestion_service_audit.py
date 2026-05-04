"""``IngestionService`` の同 tx 監査書込テスト (PR1)。

成功 path で ``pipeline_events`` に 1 行が書き込まれ、payload に集計値
(``persisted_count`` / ``staged_count`` / ``failed_codes``) が焼き付く
ことを確認する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FailureReason,
    FetchedArticle,
    FetchedMetadata,
    FetchOutcome,
    ReadyForArticle,
)
from app.collection.ingestion.ingestion_service import IngestionService
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent
from app.shared.value_objects.safe_url import SafeUrl


def _ready(source_id: int, url: str, title: str = "T") -> ReadyForArticle:
    return ReadyForArticle(
        article=FetchedArticle(
            title=title,
            body="x" * 100,
            published_at=PublishedAt(value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)),
            source_id=source_id,
            source_url=SafeUrl(url),
        ),
        metadata=FetchedMetadata(language="en-US", site_name="VentureBeat"),
    )


class _StubFetcher:
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "site_name"})

    def __init__(self, outcomes: list[FetchOutcome]) -> None:
        self._outcomes = outcomes

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        for o in self._outcomes:
            yield o


@pytest.fixture
async def vb_source(db_session: AsyncSession) -> NewsSource:
    source = NewsSource(
        name="VentureBeat",
        source_type=SourceType.RSS,
        site_url="https://venturebeat.com",
        endpoint_url="https://venturebeat.com/feed/",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest.mark.asyncio
async def test_success_writes_succeeded_event(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    ready = _ready(vb_source.id, "https://venturebeat.com/a/")
    svc = IngestionService(session_factory, lambda: _StubFetcher([ready]))

    await svc.execute(vb_source.id, attempt=1)

    events = (await db_session.execute(select(PipelineEvent))).scalars().all()
    assert len(events) == 1
    e = events[0]
    assert e.stage == "source_fetch"
    assert e.event_type == "succeeded"
    assert e.outcome_code == "fetched"
    assert e.source_id == vb_source.id
    assert e.attempt == 1
    assert e.duration_ms is not None and e.duration_ms >= 0
    assert e.payload["fetcher_class"] == "_StubFetcher"
    assert e.payload["persisted_count"] == 1
    assert e.payload["staged_count"] == 0
    assert e.payload["failed_codes"] is None  # 失敗 0 件 → None で一貫


@pytest.mark.asyncio
async def test_failed_codes_aggregated_in_payload(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Failed entry の reason.code 別カウントが payload.failed_codes に集計。"""
    fa = Failed(reason=FailureReason(code="body_too_short", retryable=False))
    fb = Failed(reason=FailureReason(code="title_missing", retryable=False))
    fc = Failed(reason=FailureReason(code="body_too_short", retryable=False))
    svc = IngestionService(session_factory, lambda: _StubFetcher([fa, fb, fc]))

    await svc.execute(vb_source.id, attempt=1)

    e = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert e.outcome_code == "fetched"  # 成功は 1 本 (件数で分けない)
    assert e.payload["persisted_count"] == 0
    assert e.payload["failed_count"] == 3
    assert e.payload["failed_codes"] == {
        "body_too_short": 2,
        "title_missing": 1,
    }


@pytest.mark.asyncio
async def test_attempt_passthrough_to_payload_event(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Task から渡した attempt 値がそのまま pipeline_events.attempt に載る。"""
    ready = _ready(vb_source.id, "https://venturebeat.com/a/")
    svc = IngestionService(session_factory, lambda: _StubFetcher([ready]))

    await svc.execute(vb_source.id, attempt=3)

    e = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert e.attempt == 3
