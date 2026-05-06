"""``IngestionService`` の振り分け責務テスト (PR2.5-B 仕様)。

PR2.5-B cutover で ``discovered_articles`` 経路は撤去され、新 3 表
(``article_urls`` / ``articles`` / ``pending_html_articles``) を直接駆動する。

検証する不変条件:

- Pattern R (``ReadyForArticle``): ``article_urls`` upsert + ``articles`` 直 INSERT、
  ``IngestedOutcome.persisted`` に Entity が積まれる
- Pattern H (``PendingHtmlFetch``): ``article_urls`` upsert + ``pending_html_articles``
  作成、Outcome は純化されているため caller には何も渡らない (cron poller が DB 駆動)
- ``Failed`` は永続化に流れない (silent skip しない、payload で観測する)
- 同 URL の重複 yield は ``article_urls`` UNIQUE で 1 件に絞られる
- 既知 URL (``ON CONFLICT DO NOTHING`` で id 取れない) は ``known_url`` skip
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
from app.models.article_url import ArticleUrl as ArticleUrlORM
from app.models.news_source import NewsSource, SourceType
from app.models.pending_html_article import PendingHtmlArticle as PendingHtmlArticleORM
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
async def test_pattern_r_inserts_article_url_and_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Pattern R は article_urls + articles を 1 件ずつ作る。"""
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [_ready_entry(vb_source.id, "https://venturebeat.com/a/")]
        ),
    )

    outcome = await svc.execute(vb_source.id)

    assert isinstance(outcome, IngestedOutcome)
    assert len(outcome.persisted) == 1
    assert outcome.persisted[0].article_url_id is not None

    urls = (await db_session.execute(select(ArticleUrlORM))).scalars().all()
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert len(urls) == 1
    assert len(articles) == 1
    assert articles[0].article_url_id == urls[0].id
    assert articles[0].discovered_article_id is None
    assert pendings == []


@pytest.mark.asyncio
async def test_pattern_h_inserts_article_url_and_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Pattern H は article_urls + pending_html_articles を作り Outcome に積まない。"""
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [_pending_entry(vb_source.id, "https://techcrunch.com/h/")]
        ),
    )

    outcome = await svc.execute(vb_source.id)

    assert outcome.persisted == []  # Pattern H は cron poller 駆動、Outcome に乗らない

    urls = (await db_session.execute(select(ArticleUrlORM))).scalars().all()
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert len(urls) == 1
    assert articles == []
    assert len(pendings) == 1
    assert pendings[0].article_url_id == urls[0].id
    assert pendings[0].status == "open"
    assert pendings[0].attempt_count == 0


@pytest.mark.asyncio
async def test_failed_does_not_persist(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Failed は永続化に流れず、payload (failed_codes) に集計されるのみ。"""
    failed = Failed(reason=FailureReason(code="body_too_short", retryable=False))
    svc = IngestionService(session_factory, lambda: _StubFetcher([failed]))

    outcome = await svc.execute(vb_source.id)

    assert outcome.persisted == []
    urls = (await db_session.execute(select(ArticleUrlORM))).scalars().all()
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert urls == []
    assert articles == []
    assert pendings == []


@pytest.mark.asyncio
async def test_duplicate_url_yielded_twice_persists_once(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """同 URL の重複 yield は article_urls UNIQUE で 1 件に絞られ、
    2 度目は known_url skip となる (article_url_id が None)。"""
    e1 = _ready_entry(vb_source.id, "https://venturebeat.com/dup/")
    e2 = _ready_entry(vb_source.id, "https://venturebeat.com/dup/")
    svc = IngestionService(session_factory, lambda: _StubFetcher([e1, e2]))

    outcome = await svc.execute(vb_source.id)

    assert len(outcome.persisted) == 1
    urls = (await db_session.execute(select(ArticleUrlORM))).scalars().all()
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(urls) == 1
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_canonicalization_dedupes_tracking_query(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """canonicalize_url が tracking parameter / trailing slash を吸収するため、
    異なる原始 URL でも normalized が同じなら 2 度目は known_url skip。"""
    e1 = _ready_entry(vb_source.id, "https://venturebeat.com/a")
    e2 = _ready_entry(vb_source.id, "https://venturebeat.com/a/?utm_source=twitter")
    svc = IngestionService(session_factory, lambda: _StubFetcher([e1, e2]))

    outcome = await svc.execute(vb_source.id)

    # 2 件目は normalized URL が同じになり ON CONFLICT DO NOTHING で id 取れず skip
    assert len(outcome.persisted) == 1
    urls = (await db_session.execute(select(ArticleUrlORM))).scalars().all()
    assert len(urls) == 1


@pytest.mark.asyncio
async def test_mixed_ready_pending_failed_route_independently(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """混在 (R + H + Failed) でも各経路が独立して正しく分岐する。"""
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
    urls = (await db_session.execute(select(ArticleUrlORM))).scalars().all()
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert len(urls) == 2  # R + H
    assert len(articles) == 1  # R only
    assert len(pendings) == 1  # H only
