"""``IngestionService`` 結合テスト (collection-acquisition-redesign Phase 1a')。

Fetcher は stub に差し替え、Service が ``discovered_articles`` + ``articles``
を正しく永続化することをテスト DB 経由で確認する。
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
    FetchedArticle,
    FetchedMetadata,
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


def _ready(source_id: int, url: str, title: str = "Test Title") -> ReadyForArticle:
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


def _pending(source_id: int, url: str, title: str = "TC Title") -> PendingHtmlFetch:
    return PendingHtmlFetch(
        title=title,
        source_id=source_id,
        source_url=SafeUrl(url),
        published_at_hint=PublishedAt(
            value=datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
        ),
        metadata=FetchedMetadata(language="en-US", site_name="TechCrunch"),
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
async def test_ready_persists_discovered_and_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    ready = _ready(vb_source.id, "https://venturebeat.com/article-1/")
    svc = IngestionService(session_factory, lambda: _StubFetcher([ready]))

    outcome = await svc.execute(vb_source.id)

    assert isinstance(outcome, IngestedOutcome)
    assert len(outcome.persisted) == 1
    article = outcome.persisted[0]
    assert article.title == "Test Title"
    assert article.id > 0
    assert article.discovered_article_id > 0

    # DB に row が 1 件ずつあることを確認
    discovered_rows = (
        (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    )
    article_rows = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(discovered_rows) == 1
    assert len(article_rows) == 1
    assert article_rows[0].original_title == "Test Title"


@pytest.mark.asyncio
async def test_failed_increments_counter_no_persist(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    failed = Failed(reason=FailureReason(code="body_too_short", retryable=False))
    svc = IngestionService(session_factory, lambda: _StubFetcher([failed]))

    outcome = await svc.execute(vb_source.id)

    # Outcome 純化: failed_count は pipeline_events.payload で確認
    # (詳細は test_ingestion_service_audit.py)
    assert isinstance(outcome, IngestedOutcome)
    assert len(outcome.persisted) == 0
    rows = (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_duplicate_url_persists_once(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """同 URL が 2 回 yield されても DB には 1 件だけ永続化 (race recovery)。"""
    r1 = _ready(vb_source.id, "https://venturebeat.com/dup/")
    r2 = _ready(vb_source.id, "https://venturebeat.com/dup/")
    svc = IngestionService(session_factory, lambda: _StubFetcher([r1, r2]))

    outcome = await svc.execute(vb_source.id)

    assert isinstance(outcome, IngestedOutcome)
    discovered_rows = (
        (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    )
    article_rows = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(discovered_rows) == 1
    assert len(article_rows) == 1


@pytest.mark.asyncio
async def test_pending_creates_discovered_only_and_stages(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """PendingHtmlFetch は discovered のみ作成し staged に積む (Pattern H)。"""
    pending = _pending(vb_source.id, "https://techcrunch.com/article-h/")
    svc = IngestionService(session_factory, lambda: _StubFetcher([pending]))

    outcome = await svc.execute(vb_source.id)

    assert isinstance(outcome, IngestedOutcome)
    assert len(outcome.persisted) == 0  # Article は 2 段目で作る
    assert len(outcome.staged) == 1
    assert outcome.staged[0].pending.title == "TC Title"
    assert outcome.staged[0].discovered_id > 0

    # DB: discovered 行のみ作成されている
    discovered_rows = (
        (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    )
    article_rows = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(discovered_rows) == 1
    assert len(article_rows) == 0


@pytest.mark.asyncio
async def test_mixed_ready_pending_and_failed(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Pattern R / Pattern H / Failed が混在しても各経路が正しく分岐する。"""
    ready = _ready(vb_source.id, "https://venturebeat.com/ok/")
    pending = _pending(vb_source.id, "https://techcrunch.com/h/")
    failed = Failed(reason=FailureReason(code="title_missing", retryable=False))
    svc = IngestionService(
        session_factory, lambda: _StubFetcher([ready, pending, failed])
    )

    outcome = await svc.execute(vb_source.id)

    assert isinstance(outcome, IngestedOutcome)
    assert len(outcome.persisted) == 1
    assert len(outcome.staged) == 1
    article_rows = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(article_rows) == 1  # Pattern R 分のみ
    discovered_rows = (
        (await db_session.execute(select(DiscoveredArticleORM))).scalars().all()
    )
    assert len(discovered_rows) == 2  # R + H


@pytest.mark.asyncio
async def test_mixed_ready_and_failed(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    ready = _ready(vb_source.id, "https://venturebeat.com/ok/")
    failed = Failed(reason=FailureReason(code="title_missing", retryable=False))
    svc = IngestionService(session_factory, lambda: _StubFetcher([ready, failed]))

    outcome = await svc.execute(vb_source.id)

    assert isinstance(outcome, IngestedOutcome)
    assert len(outcome.persisted) == 1
    rows = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(rows) == 1
