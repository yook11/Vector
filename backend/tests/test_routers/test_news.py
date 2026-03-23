"""Tests for /api/v1/news router endpoints."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_model import AIModel
from app.models.analysis import AnalysisResult, AnalysisTranslation, Sentiment
from app.models.associations import NewsKeyword
from app.models.keyword import Keyword
from app.models.news import NewsArticle
from app.models.news_source import NewsSource


async def _create_article(
    session: AsyncSession,
    title: str = "Test Article",
    url: str = "https://example.com/article",
    source: str = "Test Source",
    published_at: datetime | None = None,
    source_id: int | None = None,
) -> NewsArticle:
    """Helper to create a news article."""
    article = NewsArticle(
        title_original=title,
        url=url,
        source=source,
        published_at=published_at or datetime.now(UTC),
        fetched_at=datetime.now(UTC),
        source_id=source_id,
    )
    session.add(article)
    await session.commit()
    await session.refresh(article)
    return article


async def _create_analysis(
    session: AsyncSession,
    article: NewsArticle,
    ai_model_id: int,
    sentiment: Sentiment = Sentiment.POSITIVE,
    impact_score: int = 7,
) -> AnalysisResult:
    """Helper to create an analysis result with translation."""
    analysis = AnalysisResult(
        news_article_id=article.id,
        ai_model_id=ai_model_id,
        sentiment=sentiment,
        impact_score=impact_score,
        reasoning="Test reasoning",
        analyzed_at=datetime.now(UTC),
    )
    session.add(analysis)
    await session.flush()

    translation = AnalysisTranslation(
        analysis_id=analysis.id,
        locale="ja",
        title="テスト記事",
        summary="テストの要約",
    )
    session.add(translation)
    await session.commit()
    await session.refresh(analysis)
    return analysis


@pytest.mark.asyncio
class TestListNews:
    async def test_empty_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/news")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["totalPages"] == 0

    async def test_returns_articles(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_article(db_session, url="https://example.com/1")
        await _create_article(db_session, url="https://example.com/2")

        resp = await client.get("/api/v1/news")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    async def test_pagination(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        for i in range(5):
            await _create_article(db_session, url=f"https://example.com/{i}")

        resp = await client.get("/api/v1/news?page=1&perPage=2")
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
    ) -> None:
        article = await _create_article(db_session, url="https://example.com/kw")
        link = NewsKeyword(news_article_id=article.id, keyword_id=sample_keyword.id)
        db_session.add(link)
        await db_session.commit()

        # Also create an unlinked article
        await _create_article(db_session, url="https://example.com/other")

        resp = await client.get(f"/api/v1/news?keywordId={sample_keyword.id}")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["titleOriginal"] == "Test Article"

    async def test_filter_by_sentiment(
        self, client: AsyncClient, db_session: AsyncSession, sample_ai_model: AIModel
    ) -> None:
        a1 = await _create_article(db_session, url="https://example.com/pos")
        await _create_analysis(db_session, a1, sample_ai_model.id, sentiment=Sentiment.POSITIVE)

        a2 = await _create_article(db_session, url="https://example.com/neg")
        await _create_analysis(db_session, a2, sample_ai_model.id, sentiment=Sentiment.NEGATIVE)

        resp = await client.get("/api/v1/news?sentiment=positive")
        data = resp.json()
        assert data["total"] == 1

    async def test_filter_by_min_impact(
        self, client: AsyncClient, db_session: AsyncSession, sample_ai_model: AIModel
    ) -> None:
        a1 = await _create_article(db_session, url="https://example.com/high")
        await _create_analysis(db_session, a1, sample_ai_model.id, impact_score=9)

        a2 = await _create_article(db_session, url="https://example.com/low")
        await _create_analysis(db_session, a2, sample_ai_model.id, impact_score=3)

        resp = await client.get("/api/v1/news?minImpact=7")
        data = resp.json()
        assert data["total"] == 1

    async def test_sort_by_published_at_desc(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        await _create_article(
            db_session,
            title="Older",
            url="https://example.com/old",
            published_at=now - timedelta(days=2),
        )
        await _create_article(
            db_session,
            title="Newer",
            url="https://example.com/new",
            published_at=now,
        )

        resp = await client.get("/api/v1/news?sortBy=publishedAt&sortOrder=desc")
        items = resp.json()["items"]
        assert items[0]["titleOriginal"] == "Newer"
        assert items[1]["titleOriginal"] == "Older"

    async def test_filter_by_source_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        await _create_article(
            db_session,
            url="https://example.com/src1",
            source_id=sample_source.id,
        )
        await _create_article(db_session, url="https://example.com/src2")

        resp = await client.get(f"/api/v1/news?sourceId={sample_source.id}")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["url"] == "https://example.com/src1"

    async def test_filter_by_source_id_nonexistent(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_article(db_session)

        resp = await client.get("/api/v1/news?sourceId=99999")
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_camel_case_response(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _create_article(db_session)
        resp = await client.get("/api/v1/news")
        data = resp.json()
        assert "totalPages" in data
        assert "perPage" in data
        item = data["items"][0]
        assert "titleOriginal" in item
        assert "publishedAt" in item
        assert "fetchedAt" in item


@pytest.mark.asyncio
class TestGetNews:
    async def test_get_existing(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        article = await _create_article(db_session)
        resp = await client.get(f"/api/v1/news/{article.id}")
        assert resp.status_code == 200
        assert resp.json()["titleOriginal"] == "Test Article"

    async def test_get_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/news/99999")
        assert resp.status_code == 404

    async def test_get_with_analysis(
        self, client: AsyncClient, db_session: AsyncSession, sample_ai_model: AIModel
    ) -> None:
        article = await _create_article(db_session)
        await _create_analysis(db_session, article, sample_ai_model.id)

        resp = await client.get(f"/api/v1/news/{article.id}")
        data = resp.json()
        assert data["analysis"] is not None
        assert data["analysis"]["title"] == "テスト記事"
        assert data["analysis"]["sentiment"] == "positive"

    async def test_get_with_keywords(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_keyword: Keyword,
    ) -> None:
        article = await _create_article(db_session)
        link = NewsKeyword(news_article_id=article.id, keyword_id=sample_keyword.id)
        db_session.add(link)
        await db_session.commit()

        resp = await client.get(f"/api/v1/news/{article.id}")
        data = resp.json()
        assert len(data["keywords"]) == 1
        assert data["keywords"][0]["keyword"] == "Quantum Computing"


@pytest.mark.asyncio
class TestFetchNews:
    async def test_fetch_returns_202(self, admin_client: AsyncClient) -> None:
        mock_task_handle = AsyncMock()
        mock_task_handle.task_id = "test-task-id-123"

        with patch(
            "app.routers.news.fetch_and_analyze_task",
        ) as mock_task:
            mock_task.kiq = AsyncMock(return_value=mock_task_handle)
            resp = await admin_client.post("/api/v1/news/fetch")

        assert resp.status_code == 202
        data = resp.json()
        assert data["jobId"] == "test-task-id-123"
        assert data["message"] == "Fetch task submitted"
        assert data["sourcesCount"] is None  # all due sources
        mock_task.kiq.assert_called_once_with(source_ids=None)

    async def test_fetch_with_source_ids(self, admin_client: AsyncClient) -> None:
        mock_task_handle = AsyncMock()
        mock_task_handle.task_id = "test-task-id-456"

        with patch(
            "app.routers.news.fetch_and_analyze_task",
        ) as mock_task:
            mock_task.kiq = AsyncMock(return_value=mock_task_handle)
            resp = await admin_client.post(
                "/api/v1/news/fetch",
                json={"sourceIds": [1, 2, 3]},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["sourcesCount"] == 3
        mock_task.kiq.assert_called_once_with(source_ids=[1, 2, 3])
