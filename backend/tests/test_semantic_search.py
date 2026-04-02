"""Tests for semantic search (q parameter on GET /api/v1/news)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource, SourceType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_EMBEDDING_A = [0.1] * 768  # "close" to query
FAKE_EMBEDDING_B = [0.9] * 768  # "far" from query
FAKE_QUERY_EMBEDDING = [0.1] * 768  # matches A


async def _create_source(db_session: AsyncSession) -> NewsSource:
    source = NewsSource(
        name="Test Source",
        source_type=SourceType.RSS,
        site_url="https://example.com",
        endpoint_url="https://example.com/feed.xml",
    )
    db_session.add(source)
    await db_session.flush()
    return source


async def _create_article(
    db_session: AsyncSession,
    source: NewsSource,
    *,
    title: str = "Test Article",
    url: str = "https://example.com/1",
    embedding: list[float] | None = None,
) -> NewsArticle:
    article = NewsArticle(
        original_title=title,
        original_url=url,
        news_source_id=source.id,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()

    # Always create ArticleAnalysis (required by INNER JOIN); add embedding if provided
    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title=f"Translated: {title}",
        summary="Test summary",
        impact_level=ImpactLevel.MEDIUM,
        reasoning="Test reasoning",
        ai_model="gemini-2.0-flash",
        embedding=embedding,
        embedding_model="text-embedding-004" if embedding else None,
    )
    db_session.add(analysis)
    await db_session.flush()

    return article


def _patch_embed_query(return_value: list[float] = FAKE_QUERY_EMBEDDING):
    """Patch embed_search_query to return a fixed vector."""
    return patch(
        "app.services.news.embed_search_query",
        new_callable=AsyncMock,
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# A. Basic semantic search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_returns_articles_with_embedding(
    authed_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """GET /api/v1/news?q=test should return articles with embeddings."""
    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        title="AI Breakthrough",
        url="https://example.com/ai",
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    with _patch_embed_query():
        resp = await authed_client.get("/api/v1/news", params={"q": "AI research"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(
        "AI Breakthrough" in item["translatedTitle"] for item in data["items"]
    )


@pytest.mark.asyncio
async def test_semantic_search_excludes_articles_without_embedding(
    authed_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Articles without embeddings should be excluded from semantic search results."""
    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        title="With Embedding",
        url="https://example.com/with",
        embedding=FAKE_EMBEDDING_A,
    )
    await _create_article(
        db_session,
        source,
        title="Without Embedding",
        url="https://example.com/without",
        embedding=None,
    )
    await db_session.commit()

    with _patch_embed_query():
        resp = await authed_client.get("/api/v1/news", params={"q": "test"})

    assert resp.status_code == 200
    data = resp.json()
    titles = [item["translatedTitle"] for item in data["items"]]
    assert "Translated: With Embedding" in titles
    assert "Translated: Without Embedding" not in titles


# ---------------------------------------------------------------------------
# B. Combined with existing filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_combined_with_source_filter(
    authed_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Semantic search should work with sourceId filter."""
    source_a = await _create_source(db_session)
    source_b = NewsSource(
        name="Other Source",
        source_type=SourceType.RSS,
        site_url="https://other.com",
        endpoint_url="https://other.com/feed.xml",
    )
    db_session.add(source_b)
    await db_session.flush()

    await _create_article(
        db_session,
        source_a,
        title="Source A Article",
        url="https://example.com/a",
        embedding=FAKE_EMBEDDING_A,
    )
    await _create_article(
        db_session,
        source_b,
        title="Source B Article",
        url="https://other.com/b",
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    with _patch_embed_query():
        resp = await authed_client.get(
            "/api/v1/news",
            params={"q": "test", "sourceId": source_a.id},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["translatedTitle"] == "Translated: Source A Article"


# ---------------------------------------------------------------------------
# C. No q parameter -- existing behavior unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_q_parameter_returns_analyzed_articles(
    authed_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Without q parameter, only analyzed articles are returned."""
    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        title="Article 1",
        url="https://example.com/1",
        embedding=FAKE_EMBEDDING_A,
    )
    await _create_article(
        db_session,
        source,
        title="Article 2",
        url="https://example.com/2",
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    # No patching needed -- embed_search_query should not be called
    resp = await authed_client.get("/api/v1/news")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


# ---------------------------------------------------------------------------
# D. Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_returns_503_on_embedding_failure(
    authed_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """When embedding generation fails, return 503."""
    from app.services.embedding import EmbeddingError

    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    with patch(
        "app.services.news.embed_search_query",
        new_callable=AsyncMock,
        side_effect=EmbeddingError("API down"),
    ):
        resp = await authed_client.get("/api/v1/news", params={"q": "test"})

    assert resp.status_code == 503
    assert "embedding" in resp.json()["detail"].lower()
