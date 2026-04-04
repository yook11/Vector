"""Tests for /api/v1/articles router endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.article_keyword import ArticleKeyword
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource


async def _create_article(
    session: AsyncSession,
    source: NewsSource,
    title: str = "Test Article",
    url: str = "https://example.com/article",
    published_at: datetime | None = None,
) -> NewsArticle:
    """Helper to create a news article."""
    article = NewsArticle(
        original_title=title,
        original_url=url,
        news_source_id=source.id,
        published_at=published_at or datetime.now(UTC),
    )
    session.add(article)
    await session.commit()
    await session.refresh(article)
    return article


async def _create_analysis(
    session: AsyncSession,
    article: NewsArticle,
    impact_level: ImpactLevel = ImpactLevel.HIGH,
    translated_title: str = "テスト記事",
) -> ArticleAnalysis:
    """Helper to create an analysis result."""
    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title=translated_title,
        summary="テストの要約",
        impact_level=impact_level,
        reasoning="Test reasoning",
        ai_model="gemini-2.0-flash",
    )
    session.add(analysis)
    await session.commit()
    await session.refresh(analysis)
    return analysis


@pytest.mark.asyncio
class TestListArticles:
    async def test_empty_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/articles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["totalPages"] == 0

    async def test_returns_analyzed_articles(
        self, client: AsyncClient, db_session: AsyncSession, sample_source: NewsSource
    ) -> None:
        a1 = await _create_article(db_session, sample_source, url="https://example.com/1")
        await _create_analysis(db_session, a1)
        a2 = await _create_article(db_session, sample_source, url="https://example.com/2")
        await _create_analysis(db_session, a2)
        # Unanalyzed article should be excluded
        await _create_article(db_session, sample_source, url="https://example.com/3")

        resp = await client.get("/api/v1/articles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    async def test_pagination(
        self, client: AsyncClient, db_session: AsyncSession, sample_source: NewsSource
    ) -> None:
        for i in range(5):
            article = await _create_article(
                db_session, sample_source, url=f"https://example.com/{i}"
            )
            await _create_analysis(db_session, article)

        resp = await client.get("/api/v1/articles?page=1&perPage=2")
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["page"] == 1
        assert data["perPage"] == 2
        assert data["totalPages"] == 3

    async def test_filter_by_keyword(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_keyword: Keyword,
        sample_source: NewsSource,
    ) -> None:
        article = await _create_article(
            db_session, sample_source, url="https://example.com/kw"
        )
        await _create_analysis(db_session, article)
        link = ArticleKeyword(news_article_id=article.id, keyword_id=sample_keyword.id)
        db_session.add(link)
        await db_session.commit()

        # Unlinked + analyzed article
        other = await _create_article(
            db_session, sample_source, url="https://example.com/other"
        )
        await _create_analysis(db_session, other)

        resp = await client.get(f"/api/v1/articles?keywordId={sample_keyword.id}")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["translatedTitle"] == "テスト記事"

    async def test_filter_by_impact_level(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        a1 = await _create_article(
            db_session, sample_source, url="https://example.com/high"
        )
        await _create_analysis(db_session, a1, impact_level=ImpactLevel.HIGH)

        a2 = await _create_article(
            db_session, sample_source, url="https://example.com/low"
        )
        await _create_analysis(db_session, a2, impact_level=ImpactLevel.LOW)

        resp = await client.get("/api/v1/articles?impactLevel=high")
        data = resp.json()
        assert data["total"] == 1

    async def test_sort_by_published_at_desc(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        now = datetime.now(UTC)
        older = await _create_article(
            db_session,
            sample_source,
            title="Older",
            url="https://example.com/old",
            published_at=now - timedelta(days=2),
        )
        await _create_analysis(
            db_session, older, translated_title="古い記事"
        )
        newer = await _create_article(
            db_session,
            sample_source,
            title="Newer",
            url="https://example.com/new",
            published_at=now,
        )
        await _create_analysis(
            db_session, newer, translated_title="新しい記事"
        )

        resp = await client.get("/api/v1/articles?sortBy=publishedAt&sortOrder=desc")
        items = resp.json()["items"]
        assert items[0]["translatedTitle"] == "新しい記事"
        assert items[1]["translatedTitle"] == "古い記事"

    async def test_filter_by_source_name(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        a1 = await _create_article(
            db_session,
            sample_source,
            url="https://example.com/src1",
        )
        await _create_analysis(db_session, a1)
        # Create a second source for the unlinked article
        second_source = NewsSource(
            name="Other Source",
            source_type="rss",
            site_url="https://other.com",
            endpoint_url="https://other.com/feed.xml",
        )
        db_session.add(second_source)
        await db_session.commit()
        await db_session.refresh(second_source)
        a2 = await _create_article(
            db_session, second_source, url="https://example.com/src2"
        )
        await _create_analysis(db_session, a2)

        resp = await client.get(
            f"/api/v1/articles?source={sample_source.name}"
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["source"]["name"] == str(sample_source.name)

    async def test_filter_by_source_name_nonexistent(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        a = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, a)

        resp = await client.get("/api/v1/articles?source=NonExistentSource")
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_camel_case_response(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        a = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, a)
        resp = await client.get("/api/v1/articles")
        data = resp.json()
        assert "totalPages" in data
        assert "perPage" in data
        item = data["items"][0]
        assert "translatedTitle" in item
        assert "summary" in item
        assert "impactLevel" in item
        assert "publishedAt" in item

    async def test_invalid_category_slug_returns_422(
        self, client: AsyncClient
    ) -> None:
        """CategorySlug VO rejects values not matching its slug pattern."""
        resp = await client.get("/api/v1/articles?category=INVALID-slug")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["loc"] == ["query", "category"]
        assert "CategorySlug" in detail[0]["msg"]

    async def test_invalid_source_name_returns_422(
        self, client: AsyncClient
    ) -> None:
        """SourceName VO rejects values containing disallowed characters."""
        resp = await client.get("/api/v1/articles?source=<bad>")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["loc"] == ["query", "source"]


@pytest.mark.asyncio
class TestGetArticle:
    async def test_get_existing(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article)
        resp = await client.get(f"/api/v1/articles/{article.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["translatedTitle"] == "テスト記事"
        assert data["original"]["title"] == "Test Article"

    async def test_get_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/articles/99999")
        assert resp.status_code == 404

    async def test_get_unanalyzed_returns_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        article = await _create_article(db_session, sample_source)
        resp = await client.get(f"/api/v1/articles/{article.id}")
        assert resp.status_code == 404

    async def test_get_with_analysis(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article)

        resp = await client.get(f"/api/v1/articles/{article.id}")
        data = resp.json()
        assert data["translatedTitle"] == "テスト記事"
        assert data["impactLevel"] == "high"
        assert data["reasoning"] == "Test reasoning"
        assert data["original"]["title"] == "Test Article"
        assert data["original"]["url"] == "https://example.com/article"

    async def test_get_with_keywords(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_keyword: Keyword,
        sample_source: NewsSource,
    ) -> None:
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article)
        link = ArticleKeyword(news_article_id=article.id, keyword_id=sample_keyword.id)
        db_session.add(link)
        await db_session.commit()

        resp = await client.get(f"/api/v1/articles/{article.id}")
        data = resp.json()
        assert len(data["keywords"]) == 1
        assert data["keywords"][0]["name"] == "Quantum Computing"
