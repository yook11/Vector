"""Tests for /api/v1/me router endpoints (subscriptions + watchlist)."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.keyword import Keyword
from app.models.news import NewsArticle


@pytest.fixture
async def second_keyword(db_session: AsyncSession) -> Keyword:
    """Create a second keyword for testing."""
    kw = Keyword(keyword="Materials Informatics", category="materials", is_active=True)
    db_session.add(kw)
    await db_session.commit()
    await db_session.refresh(kw)
    return kw


@pytest.fixture
async def sample_article(db_session: AsyncSession) -> NewsArticle:
    """Create a test news article."""
    article = NewsArticle(
        title_original="Test Article",
        url="https://example.com/test",
        source="test-source",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


@pytest.fixture
async def second_article(db_session: AsyncSession) -> NewsArticle:
    """Create a second test news article."""
    article = NewsArticle(
        title_original="Second Article",
        url="https://example.com/second",
        source="test-source",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


# --- Subscriptions ---


@pytest.mark.asyncio
class TestListSubscriptions:
    async def test_empty_list(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.get("/api/v1/me/subscriptions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []

    async def test_returns_subscriptions(
        self,
        authed_client: AsyncClient,
        sample_keyword: Keyword,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/subscriptions",
            json={"keywordId": sample_keyword.id},
        )

        resp = await authed_client.get("/api/v1/me/subscriptions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["keywordId"] == sample_keyword.id
        assert item["keyword"] == "Quantum Computing"
        assert item["category"] == "computing"
        assert "createdAt" in item

    async def test_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/me/subscriptions")
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestCreateSubscription:
    async def test_subscribe_success(
        self,
        authed_client: AsyncClient,
        sample_keyword: Keyword,
    ) -> None:
        resp = await authed_client.post(
            "/api/v1/me/subscriptions",
            json={"keywordId": sample_keyword.id},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["keywordId"] == sample_keyword.id
        assert data["keyword"] == "Quantum Computing"

    async def test_subscribe_duplicate_409(
        self,
        authed_client: AsyncClient,
        sample_keyword: Keyword,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/subscriptions",
            json={"keywordId": sample_keyword.id},
        )
        resp = await authed_client.post(
            "/api/v1/me/subscriptions",
            json={"keywordId": sample_keyword.id},
        )
        assert resp.status_code == 409

    async def test_subscribe_nonexistent_keyword_404(
        self, authed_client: AsyncClient
    ) -> None:
        resp = await authed_client.post(
            "/api/v1/me/subscriptions",
            json={"keywordId": 99999},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestDeleteSubscription:
    async def test_unsubscribe_success(
        self,
        authed_client: AsyncClient,
        sample_keyword: Keyword,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/subscriptions",
            json={"keywordId": sample_keyword.id},
        )
        resp = await authed_client.delete(
            f"/api/v1/me/subscriptions/{sample_keyword.id}"
        )
        assert resp.status_code == 204

        # Verify it's gone
        resp = await authed_client.get("/api/v1/me/subscriptions")
        assert len(resp.json()["items"]) == 0

    async def test_unsubscribe_not_found(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.delete("/api/v1/me/subscriptions/99999")
        assert resp.status_code == 404


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
            json={"newsArticleId": sample_article.id},
        )

        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["newsArticleId"] == sample_article.id
        assert item["titleOriginal"] == "Test Article"
        assert item["source"] == "test-source"
        assert "createdAt" in item

    async def test_pagination(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
        second_article: NewsArticle,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"newsArticleId": sample_article.id},
        )
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"newsArticleId": second_article.id},
        )

        resp = await authed_client.get("/api/v1/me/watchlist?perPage=1&page=1")
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 1
        assert data["totalPages"] == 2

    async def test_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/me/watchlist")
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestAddToWatchlist:
    async def test_add_success(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        resp = await authed_client.post(
            "/api/v1/me/watchlist",
            json={"newsArticleId": sample_article.id},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["newsArticleId"] == sample_article.id
        assert data["titleOriginal"] == "Test Article"

    async def test_add_duplicate_409(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"newsArticleId": sample_article.id},
        )
        resp = await authed_client.post(
            "/api/v1/me/watchlist",
            json={"newsArticleId": sample_article.id},
        )
        assert resp.status_code == 409

    async def test_add_nonexistent_article_404(
        self, authed_client: AsyncClient
    ) -> None:
        resp = await authed_client.post(
            "/api/v1/me/watchlist",
            json={"newsArticleId": 99999},
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
            json={"newsArticleId": sample_article.id},
        )
        resp = await authed_client.delete(
            f"/api/v1/me/watchlist/{sample_article.id}"
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
            json={"newsArticleId": sample_article.id},
        )

        resp = await authed_client.get("/api/v1/news")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["isWatched"] is True

    async def test_news_list_is_watched_false_when_not_in_watchlist(
        self,
        authed_client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        resp = await authed_client.get("/api/v1/news")
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["isWatched"] is False

    async def test_news_list_is_watched_false_for_unauthenticated(
        self,
        client: AsyncClient,
        sample_article: NewsArticle,
    ) -> None:
        resp = await client.get("/api/v1/news")
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["isWatched"] is False
