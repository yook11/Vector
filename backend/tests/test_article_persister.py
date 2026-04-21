"""article_persister の永続化ロジックのテスト。"""

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.ingestion.persister import (
    ArticleCandidate,
    persist_new_articles,
)
from app.domain.safe_url import SafeUrl
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource

# --- ArticleCandidate.from_external のユニットテスト ---


def test_from_external_with_valid_input() -> None:
    candidate = ArticleCandidate.from_external(
        raw_url="https://example.com/article", raw_title="Hello"
    )
    assert candidate is not None
    assert isinstance(candidate.url, SafeUrl)
    assert candidate.title == "Hello"


def test_from_external_rejects_unsafe_url() -> None:
    candidate = ArticleCandidate.from_external(
        raw_url="javascript:alert(1)", raw_title="Hello"
    )
    assert candidate is None


def test_from_external_rejects_empty_url() -> None:
    candidate = ArticleCandidate.from_external(raw_url="", raw_title="Hello")
    assert candidate is None


def test_from_external_rejects_empty_title() -> None:
    candidate = ArticleCandidate.from_external(
        raw_url="https://example.com/a", raw_title=""
    )
    assert candidate is None


def test_from_external_strips_html_tags_from_title() -> None:
    candidate = ArticleCandidate.from_external(
        raw_url="https://example.com/a", raw_title="<b>Bold</b> title"
    )
    assert candidate is not None
    assert candidate.title == "Bold title"


def test_from_external_truncates_long_title() -> None:
    long_title = "x" * 600
    candidate = ArticleCandidate.from_external(
        raw_url="https://example.com/a", raw_title=long_title
    )
    assert candidate is not None
    assert len(candidate.title) == 500


# --- Integration tests (with DB) ---


@pytest.mark.asyncio
async def test_persist_saves_new_articles(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """ArticleCandidate dict が DB に保存される。"""
    url_1 = SafeUrl("https://example.com/1")
    url_2 = SafeUrl("https://example.com/2")
    candidates = {
        url_1: ArticleCandidate(url=url_1, title="Article 1"),
        url_2: ArticleCandidate(url=url_2, title="Article 2"),
    }

    result = await persist_new_articles(db_session, sample_source, candidates)

    assert len(result.new_discovered) == 2

    await db_session.flush()
    articles = (await db_session.execute(select(DiscoveredArticle))).scalars().all()
    assert len(articles) == 2
    assert all(a.news_source_id == sample_source.id for a in articles)


@pytest.mark.asyncio
async def test_persist_skips_duplicate_urls(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """既存 URL は重複排除される。"""
    existing = DiscoveredArticle(
        original_title="Existing",
        original_url="https://example.com/existing",
        news_source_id=sample_source.id,
    )
    db_session.add(existing)
    await db_session.commit()

    url_existing = SafeUrl("https://example.com/existing")
    url_new = SafeUrl("https://example.com/new")
    candidates = {
        url_existing: ArticleCandidate(url=url_existing, title="Existing"),
        url_new: ArticleCandidate(url=url_new, title="New One"),
    }

    result = await persist_new_articles(db_session, sample_source, candidates)

    assert len(result.new_discovered) == 1


@pytest.mark.asyncio
async def test_persist_respects_max_articles_limit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """max_articles_per_fetch の上限を超えない。"""
    candidates: dict[SafeUrl, ArticleCandidate] = {}
    for i in range(60):
        url = SafeUrl(f"https://example.com/{i}")
        candidates[url] = ArticleCandidate(url=url, title=f"Article {i}")

    with patch("app.collection.ingestion.persister.settings") as mock_settings:
        mock_settings.max_articles_per_fetch = 50
        result = await persist_new_articles(db_session, sample_source, candidates)

    assert len(result.new_discovered) == 50


@pytest.mark.asyncio
async def test_persist_with_empty_candidates(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """空の候補 dict では何も保存されない。"""
    result = await persist_new_articles(db_session, sample_source, {})

    assert result.new_discovered == []
