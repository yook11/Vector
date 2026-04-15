"""Tests for /api/v1/me/watchlist router endpoints."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource


@pytest.fixture
async def sample_article(
    db_session: AsyncSession, sample_source: NewsSource
) -> NewsArticle:
    """Create a test news article with analysis."""
    article = NewsArticle(
        original_title="Test Article",
        original_url="https://example.com/test",
        news_source_id=sample_source.id,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="テスト記事",
        summary="テストの要約",
        impact_level=ImpactLevel.HIGH,
        reasoning="Test reasoning",
        ai_model="gemini-2.0-flash",
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(article, ["article_analysis"])
    return article


@pytest.fixture
async def second_article(
    db_session: AsyncSession, sample_source: NewsSource
) -> NewsArticle:
    """Create a second test news article with analysis."""
    article = NewsArticle(
        original_title="Second Article",
        original_url="https://example.com/second",
        news_source_id=sample_source.id,
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title="2番目の記事",
        summary="2番目の要約",
        impact_level=ImpactLevel.MEDIUM,
        reasoning="Second reasoning",
        ai_model="gemini-2.0-flash",
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(article, ["article_analysis"])
    return article


# --- Watchlist ---


@pytest.mark.asyncio
class TestListWatchlist:
    async def test_empty_list(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_returns_watchlist_items(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.article_analysis.id},
        )

        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["id"] == sample_article.article_analysis.id
        assert item["translatedTitle"] == "テスト記事"
        assert item["summary"] == "テストの要約"
        assert item["impactLevel"] == "high"
        assert item["source"]["name"] == "Test Tech Source"
        assert item["isWatched"] is True

    async def test_pagination(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
        second_article: NewsArticle,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.article_analysis.id},
        )
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": second_article.article_analysis.id},
        )

        resp = await authed_client.get("/api/v1/me/watchlist?perPage=1&page=1")
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 1
        assert data["totalPages"] == 2

    async def test_missing_auth_headers(self, client: AsyncClient) -> None:
        """Missing required headers return 422 (FastAPI type validation)."""
        resp = await client.get("/api/v1/me/watchlist")
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestAddToWatchlist:
    async def test_add_success(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        resp = await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.article_analysis.id},
        )
        assert resp.status_code == 201

    async def test_add_duplicate_409(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.article_analysis.id},
        )
        resp = await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.article_analysis.id},
        )
        assert resp.status_code == 409

    async def test_add_nonexistent_article_404(
        self, authed_client: AsyncClient
    ) -> None:
        resp = await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": 99999},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestRemoveFromWatchlist:
    async def test_remove_success(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.article_analysis.id},
        )
        resp = await authed_client.delete(
            f"/api/v1/me/watchlist/{sample_article.article_analysis.id}"
        )
        assert resp.status_code == 204

        # Verify it's gone
        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.json()["total"] == 0

    async def test_remove_not_found(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.delete("/api/v1/me/watchlist/99999")
        assert resp.status_code == 404


# --- News isWatched integration ---


@pytest.mark.asyncio
class TestNewsIsWatched:
    async def test_news_list_includes_is_watched(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.article_analysis.id},
        )

        resp = await authed_client.get("/api/v1/articles")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["isWatched"] is True

    async def test_news_list_is_watched_false_when_not_in_watchlist(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        resp = await authed_client.get("/api/v1/articles")
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["isWatched"] is False

    async def test_news_list_is_watched_false_for_unauthenticated(
        self,
        client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        resp = await client.get("/api/v1/articles")
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["isWatched"] is False
