"""article_persister の永続化ロジックのテスト。"""

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.ingestion.persister import (
    ArticleCandidate,
    persist_new_articles,
    to_safe_url,
)
from app.domain.safe_url import SafeUrl
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource

# --- Unit tests ---


def test_to_safe_url_with_valid_url() -> None:
    result = to_safe_url("https://example.com/article")
    assert result is not None
    assert isinstance(result, SafeUrl)


def test_to_safe_url_with_invalid_url() -> None:
    result = to_safe_url("javascript:alert(1)")
    assert result is None


def test_to_safe_url_with_empty_string() -> None:
    result = to_safe_url("")
    assert result is None


# --- Integration tests (with DB) ---


@pytest.mark.asyncio
async def test_persist_saves_new_articles(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """ArticleCandidate リストが DB に保存される。"""
    candidates = [
        ArticleCandidate(
            url=SafeUrl("https://example.com/1"),
            title="Article 1",
        ),
        ArticleCandidate(
            url=SafeUrl("https://example.com/2"),
            title="Article 2",
            description="Description 2",
        ),
    ]

    result = await persist_new_articles(db_session, sample_source, candidates)

    assert result.new_count == 2
    assert result.skipped_count == 0
    assert len(result.new_articles) == 2

    await db_session.flush()
    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert len(articles) == 2
    assert all(a.news_source_id == sample_source.id for a in articles)


@pytest.mark.asyncio
async def test_persist_skips_duplicate_urls(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """既存 URL は重複排除される。"""
    existing = NewsArticle(
        original_title="Existing",
        original_url="https://example.com/existing",
        news_source_id=sample_source.id,
    )
    db_session.add(existing)
    await db_session.commit()

    candidates = [
        ArticleCandidate(
            url=SafeUrl("https://example.com/existing"),
            title="Existing",
        ),
        ArticleCandidate(
            url=SafeUrl("https://example.com/new"),
            title="New One",
        ),
    ]

    result = await persist_new_articles(db_session, sample_source, candidates)

    assert result.new_count == 1
    assert result.skipped_count == 1


@pytest.mark.asyncio
async def test_persist_respects_max_articles_limit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """max_articles_per_fetch の上限を超えない。"""
    candidates = [
        ArticleCandidate(
            url=SafeUrl(f"https://example.com/{i}"),
            title=f"Article {i}",
        )
        for i in range(60)
    ]

    with patch("app.collection.ingestion.persister.settings") as mock_settings:
        mock_settings.max_articles_per_fetch = 50
        mock_settings.content_max_length = 8000
        result = await persist_new_articles(db_session, sample_source, candidates)

    assert result.new_count == 50


@pytest.mark.asyncio
async def test_persist_stores_content(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """content が設定されている候補は original_content に保存される。"""
    candidates = [
        ArticleCandidate(
            url=SafeUrl("https://example.com/full"),
            title="Full Content Article",
            content="A" * 600,
        ),
    ]

    result = await persist_new_articles(db_session, sample_source, candidates)

    assert result.new_count == 1
    await db_session.flush()
    articles = (await db_session.execute(select(NewsArticle))).scalars().all()
    assert articles[0].original_content is not None
    assert len(articles[0].original_content) == 600


@pytest.mark.asyncio
async def test_persist_with_empty_candidates(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """空の候補リストでは何も保存されない。"""
    result = await persist_new_articles(db_session, sample_source, [])

    assert result.new_count == 0
    assert result.skipped_count == 0
    assert result.new_articles == []


@pytest.mark.asyncio
async def test_persist_deduplicates_within_batch(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一バッチ内の重複 URL は 2 件目以降がスキップされる。"""
    candidates = [
        ArticleCandidate(
            url=SafeUrl("https://example.com/dup"),
            title="First",
        ),
        ArticleCandidate(
            url=SafeUrl("https://example.com/dup"),
            title="Second",
        ),
    ]

    result = await persist_new_articles(db_session, sample_source, candidates)

    assert result.new_count == 1
    assert result.skipped_count == 1
