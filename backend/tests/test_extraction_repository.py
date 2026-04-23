"""extraction リポジトリの統合テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.extraction.candidate import PublishedAt
from app.collection.extraction.extractor import ExtractedContent
from app.collection.extraction.repository import (
    ArticleRepository,
    DiscoveredArticleRepository,
)
from app.models.article import Article
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


async def _make_discovered(
    db_session: AsyncSession, source: NewsSource, url: str
) -> DiscoveredArticle:
    discovered = DiscoveredArticle(
        original_title="seed",
        original_url=url,
        news_source_id=source.id,
    )
    db_session.add(discovered)
    await db_session.commit()
    await db_session.refresh(discovered)
    return discovered


@pytest.mark.asyncio
async def test_find_returns_none_for_missing_id(
    db_session: AsyncSession,
) -> None:
    repo = DiscoveredArticleRepository(db_session)
    assert await repo.find(999999) is None


@pytest.mark.asyncio
async def test_find_returns_discovered_without_article(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/unextracted"
    )

    repo = DiscoveredArticleRepository(db_session)
    result = await repo.find(discovered.id)

    assert result is not None
    assert result.id == discovered.id
    assert result.original_url == SafeUrl("https://example.com/unextracted")
    assert result.article is None


@pytest.mark.asyncio
async def test_find_eager_loads_article_relation(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/extracted"
    )
    article = Article(
        discovered_article_id=discovered.id,
        original_title="seed",
        original_content="body body body body body body body body body body",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    repo = DiscoveredArticleRepository(db_session)
    result = await repo.find(discovered.id)

    assert result is not None
    assert result.article is not None
    assert result.article.id == article.id


@pytest.mark.asyncio
async def test_article_repository_create_adds_to_session(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/create"
    )
    content = ExtractedContent(
        title="Extracted",
        body="x" * 60,
        published_at=PublishedAt(datetime(2026, 3, 1, tzinfo=UTC)),
    )

    repo = ArticleRepository(db_session)
    article = repo.create(discovered.id, content)
    await db_session.flush()

    assert article.id is not None
    assert article.original_title == "Extracted"
    assert article.published_at == datetime(2026, 3, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_article_repository_create_accepts_none_published_at(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/no-date"
    )
    content = ExtractedContent(title="t", body="x" * 60, published_at=None)

    repo = ArticleRepository(db_session)
    article = repo.create(discovered.id, content)
    await db_session.flush()

    assert article.published_at is None


@pytest.mark.asyncio
async def test_article_repository_does_not_commit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """create は session.add のみ。commit は呼び出し側の責務。"""
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/no-commit"
    )
    content = ExtractedContent(title="t", body="x" * 60, published_at=None)

    repo = ArticleRepository(db_session)
    repo.create(discovered.id, content)

    # ロールバックで消える = コミットされていない
    await db_session.rollback()
    rows = (await db_session.execute(select(Article))).scalars().all()
    assert rows == []
