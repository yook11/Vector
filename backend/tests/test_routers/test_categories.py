"""/api/v1/categories ルーターエンドポイントのテスト。"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource


@pytest.mark.asyncio
class TestListCategories:
    async def test_empty_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/categories")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []

    async def test_returns_all_categories(
        self,
        client: AsyncClient,
        sample_categories: list[Category],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3

        slugs = [item["slug"] for item in data["items"]]
        assert "ai" in slugs
        assert "computing" in slugs
        assert "semiconductor" in slugs

    async def test_name_from_direct_column(
        self,
        client: AsyncClient,
        sample_categories: list[Category],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        name_map = {item["slug"]: item["name"] for item in items}
        assert name_map["ai"] == "AI"
        assert name_map["computing"] == "次世代コンピューティング"

    async def test_ordered_by_slug(
        self,
        client: AsyncClient,
        sample_categories: list[Category],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        slugs = [item["slug"] for item in items]
        assert slugs == sorted(slugs)

    async def test_no_auth_required(self, client: AsyncClient) -> None:
        """カテゴリエンドポイントは認証を要求しない。"""
        resp = await client.get("/api/v1/categories")
        assert resp.status_code == 200

    async def test_response_shape(
        self,
        client: AsyncClient,
        sample_categories: list[Category],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        item = resp.json()["items"][0]
        assert "id" not in item
        assert "slug" in item
        assert "name" in item
        # Topic 降格後 (2026-04) は CategoryDetail.topics は廃止された。
        assert "topics" not in item

    async def test_recent_count_includes_recent_analysis(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_categories: list[Category],
        sample_source: NewsSource,
    ) -> None:
        """直近 24 時間に分類された記事は recentCount に含まれる。"""
        discovered = DiscoveredArticle(
            original_title="TF Article",
            original_url="https://example.com/tf",
            news_source_id=sample_source.id,
        )
        db_session.add(discovered)
        await db_session.flush()
        article = Article(
            discovered_article_id=discovered.id,
            original_title="TF Article",
            original_content="TF content.",
        )
        db_session.add(article)
        await db_session.flush()
        extraction = ArticleExtraction(
            article_id=article.id,
            translated_title="TF記事",
            summary="要約",
            ai_model="test",
        )
        db_session.add(extraction)
        await db_session.flush()
        analysis = ArticleAnalysis(
            extraction_id=extraction.id,
            translated_title="TF記事",
            summary="要約",
            investor_take="理由",
            ai_model="test",
            topic="tensorflow",
            category_id=sample_categories[0].id,
        )
        db_session.add(analysis)
        await db_session.commit()

        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        ai_cat = next(i for i in items if i["slug"] == "ai")
        assert ai_cat["recentCount"] == 1

    async def test_recent_count_excludes_old_analysis(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_categories: list[Category],
        sample_source: NewsSource,
    ) -> None:
        """24 時間より前に分類された記事は recentCount に含まれない。"""
        discovered = DiscoveredArticle(
            original_title="TF Article Old",
            original_url="https://example.com/tf-old",
            news_source_id=sample_source.id,
        )
        db_session.add(discovered)
        await db_session.flush()
        article = Article(
            discovered_article_id=discovered.id,
            original_title="TF Article Old",
            original_content="TF content.",
        )
        db_session.add(article)
        await db_session.flush()
        extraction = ArticleExtraction(
            article_id=article.id,
            translated_title="TF記事",
            summary="要約",
            ai_model="test",
        )
        db_session.add(extraction)
        await db_session.flush()
        analysis = ArticleAnalysis(
            extraction_id=extraction.id,
            translated_title="TF記事",
            summary="要約",
            investor_take="理由",
            ai_model="test",
            topic="tensorflow",
            category_id=sample_categories[0].id,
            analyzed_at=datetime.now(UTC) - timedelta(hours=25),
        )
        db_session.add(analysis)
        await db_session.commit()

        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        ai_cat = next(i for i in items if i["slug"] == "ai")
        assert ai_cat["recentCount"] == 0
