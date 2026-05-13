"""``IngestionService`` の振り分け責務テスト。

PR-E 以降は新 2 表 (``articles`` / ``pending_html_articles``) を直接駆動する。

検証する不変条件:

- Pattern R (``ReadyForArticle``): ``articles.source_url`` (型 ``CanonicalArticleUrl``
  で canonicalize 済が構造保証) に直 INSERT、``execute()`` 戻り値の
  ``list[int]`` に永続化された article_id が積まれる
- Pattern H (``IncompleteArticle``): ``seen_repo.exists_by_source_url``
  pre-check を通過したら ``pending_html_articles.url`` で INSERT。Outcome は
  純化されているため caller には何も渡らない (cron poller が DB 駆動)
- ``SourceFetchFailed`` は永続化に流れない (silent skip しない、payload で観測する)
- 同 URL の重複 yield は ``articles.source_url UNIQUE`` で 1 件に絞られる
- ``CanonicalArticleUrl`` 型構築時点で tracking parameter / trailing slash が
  吸収される (Service 側で後付け正規化を行わない)
- 既知 URL (= articles 既存) を Pattern H で受けたら pre-check で skip
- 混在 (R + H + SourceFetchFailed) でも各経路が独立して正しく分岐する
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.outcome import (
    FetchedEntry,
    FetchOutcome,
    SourceFetchFailed,
    SourceFetchFailureReason,
)
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.collection.ingestion.ingestion_service import IngestionService
from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource, SourceType
from app.models.pending_html_article import PendingHtmlArticle as PendingHtmlArticleORM
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


def _ready_entry(source_id: int, url: str) -> FetchedEntry:
    return FetchedEntry(
        item=ReadyForArticle(
            title="Test Title",
            body="x" * 100,
            published_at=PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC)),
            source_id=source_id,
            source_url=CanonicalArticleUrl(url),
        ),
        metadata={"language": "en-US", "site_name": "VentureBeat"},
    )


def _pending_entry(source_id: int, url: str) -> FetchedEntry:
    return FetchedEntry(
        item=IncompleteArticle(
            title="TC Title",
            source_id=source_id,
            source_url=CanonicalArticleUrl(url),
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
async def test_pattern_r_inserts_canonicalized_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Pattern R は articles を 1 件作り、source_url が canonicalize 済み値で入る。"""
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [_ready_entry(vb_source.id, "https://venturebeat.com/a/")]
        ),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    assert isinstance(article_ids[0], int)

    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert len(articles) == 1
    # canonicalize で trailing slash 削除済
    assert str(articles[0].source_url) == "https://venturebeat.com/a"
    assert pendings == []


@pytest.mark.asyncio
async def test_pattern_h_inserts_pending_with_canonicalized_url(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Pattern H は pending_html_articles を作り、url は canonicalize 済み値。"""
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [_pending_entry(vb_source.id, "https://techcrunch.com/h/")]
        ),
    )

    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []  # Pattern H は cron poller 駆動

    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert articles == []
    assert len(pendings) == 1
    assert str(pendings[0].url) == "https://techcrunch.com/h"
    assert pendings[0].status == "open"
    assert pendings[0].attempt_count == 0


@pytest.mark.asyncio
async def test_pattern_h_skips_when_article_already_exists(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Pattern H pre-check: 既に articles に同 URL がある場合は pending を作らず skip。

    feed 再露出時の HTML fetch 反復を抑える実用的 idempotency の検証。
    """
    canonical = CanonicalArticleUrl("https://techcrunch.com/known")
    existing = ArticleORM(
        original_title="Already there",
        original_content="x" * 100,
        published_at=datetime(2026, 4, 1, tzinfo=UTC),
        source_id=vb_source.id,
        source_url=canonical,
    )
    db_session.add(existing)
    await db_session.commit()

    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [_pending_entry(vb_source.id, "https://techcrunch.com/known")]
        ),
    )
    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert pendings == []  # pre-check で弾かれて pending を作っていない


@pytest.mark.asyncio
async def test_failed_does_not_persist(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """SourceFetchFailed は永続化に流れず、payload (failed_codes) に集計されるのみ。"""
    failed = SourceFetchFailed(
        reason=SourceFetchFailureReason(code="body_too_short", retryable=False)
    )
    svc = IngestionService(session_factory, lambda: _StubFetcher([failed]))

    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert articles == []
    assert pendings == []


@pytest.mark.asyncio
async def test_duplicate_url_yielded_twice_persists_once(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """同 URL の重複 yield は ``articles.source_url UNIQUE`` で 1 件に絞られる。

    2 度目は ON CONFLICT DO NOTHING で ``known_url`` skip となる。
    """
    e1 = _ready_entry(vb_source.id, "https://venturebeat.com/dup/")
    e2 = _ready_entry(vb_source.id, "https://venturebeat.com/dup/")
    svc = IngestionService(session_factory, lambda: _StubFetcher([e1, e2]))

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_canonicalization_dedupes_tracking_query(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """canonicalize_url が tracking parameter / trailing slash を吸収する。

    異なる原始 URL でも canonicalize 後が同じなら ``articles.source_url UNIQUE``
    で 2 度目は弾かれ ``known_url`` skip。
    """
    e1 = _ready_entry(vb_source.id, "https://venturebeat.com/a")
    e2 = _ready_entry(vb_source.id, "https://venturebeat.com/a/?utm_source=twitter")
    svc = IngestionService(session_factory, lambda: _StubFetcher([e1, e2]))

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_mixed_ready_pending_failed_route_independently(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """混在 (R + H + SourceFetchFailed) でも各経路が独立して正しく分岐する。"""
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [
                _ready_entry(vb_source.id, "https://venturebeat.com/ok/"),
                _pending_entry(vb_source.id, "https://techcrunch.com/h/"),
                SourceFetchFailed(
                    reason=SourceFetchFailureReason(
                        code="title_missing", retryable=False
                    )
                ),
            ]
        ),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert len(articles) == 1  # R only
    assert len(pendings) == 1  # H only
