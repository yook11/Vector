"""extraction リポジトリの統合テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.extraction.candidate import (
    AlreadyExtracted,
    ArticleExtractedContent,
    DiscoveredNotFound,
    PublishedAt,
    UnextractedFound,
)
from app.collection.extraction.repository import (
    ArticleRepository,
    DiscoveredArticleRepository,
)
from app.domain.safe_url import SafeUrl
from app.models.article import Article
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource


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
async def test_lookup_returns_not_found_for_missing_id(
    db_session: AsyncSession,
) -> None:
    repo = DiscoveredArticleRepository(db_session)
    assert isinstance(await repo.lookup_for_extraction(999999), DiscoveredNotFound)


@pytest.mark.asyncio
async def test_lookup_returns_unextracted_when_article_absent(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/unextracted"
    )

    repo = DiscoveredArticleRepository(db_session)
    result = await repo.lookup_for_extraction(discovered.id)

    assert isinstance(result, UnextractedFound)
    assert result.article.id == discovered.id
    assert result.article.url == SafeUrl("https://example.com/unextracted")


@pytest.mark.asyncio
async def test_lookup_returns_already_extracted_when_article_exists(
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
    result = await repo.lookup_for_extraction(discovered.id)

    assert isinstance(result, AlreadyExtracted)
    assert result.article_id == article.id


@pytest.mark.asyncio
async def test_article_repository_create_adds_to_session(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    discovered = await _make_discovered(
        db_session, sample_source, "https://example.com/create"
    )
    content = ArticleExtractedContent(
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
    content = ArticleExtractedContent(title="t", body="x" * 60, published_at=None)

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
    content = ArticleExtractedContent(title="t", body="x" * 60, published_at=None)

    repo = ArticleRepository(db_session)
    repo.create(discovered.id, content)

    # ロールバックで消える = コミットされていない
    await db_session.rollback()
    rows = (await db_session.execute(select(Article))).scalars().all()
    assert rows == []
