"""``IngestionService`` の永続化責務テスト。

検証する不変条件:

- Pattern R (`ReadyForArticle`) は ``discovered_articles`` + ``articles`` を 1 件ずつ
  永続化する
- Pattern H (`PendingHtmlFetch`) は ``discovered_articles`` のみ作成し、Stage 2
  への引き渡し用に ``StagedArticle`` を返す
- ``Failed`` は永続化に流れない (silent skip しない、payload で観測する)
- 同 URL の重複 yield は DB に 1 件だけ落ちる (race recovery)
- 混在 (R + H + Failed) でも各経路が独立して正しく分岐する
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FailureReason,
    FetchedEntry,
    FetchOutcome,
    PendingHtmlFetch,
    ReadyForArticle,
)
from app.collection.ingestion.ingestion_service import (
    IngestedOutcome,
    IngestionService,
)
from app.models.article import Article as ArticleORM
from app.models.discovered_article import DiscoveredArticle as DiscoveredArticleORM
from app.models.news_source import NewsSource, SourceType
from app.shared.value_objects.safe_url import SafeUrl


def _ready_entry(source_id: int, url: str) -> FetchedEntry:
    return FetchedEntry(
        item=ReadyForArticle(
            title="Test Title",
            body="x" * 100,
            published_at=PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC)),
            source_id=source_id,
            source_url=SafeUrl(url),
        ),
        metadata={"language": "en-US", "site_name": "VentureBeat"},
    )


def _pending_entry(source_id: int, url: str) -> FetchedEntry:
    return FetchedEntry(
        item=PendingHtmlFetch(
            title="TC Title",
            source_id=source_id,
            source_url=SafeUrl(url),
            published_at_hint=PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC)),
        ),
        metadata={"language": "en-US", "site_name": "TechCrunch"},
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
async def test_pattern_r_persists_discovered_and_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [_ready_entry(vb_source.id, "https://venturebeat.com/a/")]
        ),
    )

    outcome = await svc.execute(vb_source.id)

    assert isinstance(outcome, IngestedOutcome)
    assert len(outcome.persisted) == 1
    discovered = (
        (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    )
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(discovered) == 1
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_pattern_h_creates_discovered_and_stages_for_stage_two(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [_pending_entry(vb_source.id, "https://techcrunch.com/h/")]
        ),
    )

    outcome = await svc.execute(vb_source.id)

    assert len(outcome.persisted) == 0
    assert len(outcome.staged) == 1
    discovered = (
        (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    )
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(discovered) == 1
    assert len(articles) == 0


@pytest.mark.asyncio
async def test_failed_does_not_persist(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    failed = Failed(reason=FailureReason(code="body_too_short", retryable=False))
    svc = IngestionService(session_factory, lambda: _StubFetcher([failed]))

    outcome = await svc.execute(vb_source.id)

    assert outcome.persisted == [] and outcome.staged == []
    rows = (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_duplicate_url_yielded_twice_persists_once(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    e1 = _ready_entry(vb_source.id, "https://venturebeat.com/dup/")
    e2 = _ready_entry(vb_source.id, "https://venturebeat.com/dup/")
    svc = IngestionService(session_factory, lambda: _StubFetcher([e1, e2]))

    await svc.execute(vb_source.id)

    discovered = (
        (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    )
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(discovered) == 1
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_mixed_ready_pending_failed_route_independently(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [
                _ready_entry(vb_source.id, "https://venturebeat.com/ok/"),
                _pending_entry(vb_source.id, "https://techcrunch.com/h/"),
                Failed(reason=FailureReason(code="title_missing", retryable=False)),
            ]
        ),
    )

    outcome = await svc.execute(vb_source.id)

    assert len(outcome.persisted) == 1
    assert len(outcome.staged) == 1
    discovered = (
        (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    )
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(discovered) == 2  # R + H
    assert len(articles) == 1  # R only
