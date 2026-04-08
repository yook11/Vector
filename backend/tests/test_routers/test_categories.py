"""Tests for /api/v1/categories router endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_analysis import ArticleAnalysis
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
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
        assert "ai_ml" in slugs
        assert "quantum" in slugs
        assert "semiconductor" in slugs

    async def test_name_from_direct_column(
        self,
        client: AsyncClient,
        sample_categories: list[Category],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        name_map = {item["slug"]: item["name"] for item in items}
        assert name_map["ai_ml"] == "AI・ML"
        assert name_map["quantum"] == "量子コンピュータ"

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
        """Categories endpoint should not require authentication."""
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

    async def test_article_count(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_categories: list[Category],
        sample_source: NewsSource,
    ) -> None:
        """Category should include article count from linked keywords."""
        kw = Keyword(name="TensorFlow", category_id=sample_categories[0].id)
        db_session.add(kw)
        await db_session.flush()

        from app.models.news_article import NewsArticle

        article = NewsArticle(
            original_title="TF Article",
            original_url="https://example.com/tf",
            news_source_id=sample_source.id,
        )
        db_session.add(article)
        await db_session.flush()

        analysis = ArticleAnalysis(
            news_article_id=article.id,
            translated_title="TF記事",
            summary="要約",
            impact_level="high",
            reasoning="理由",
            ai_model="test",
        )
        db_session.add(analysis)
        await db_session.flush()

        nk = ArticleKeyword(article_analysis_id=analysis.id, keyword_id=kw.id)
        db_session.add(nk)
        await db_session.commit()

        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        ai_ml = next(i for i in items if i["slug"] == "ai_ml")
        assert ai_ml["articleCount"] == 1

    async def test_nested_keywords(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_categories: list[Category],
    ) -> None:
        """Category response should include nested keywords."""
        kw = Keyword(name="PyTorch", category_id=sample_categories[0].id)
        db_session.add(kw)
        await db_session.commit()

        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        ai_ml = next(i for i in items if i["slug"] == "ai_ml")
        assert len(ai_ml["keywords"]) == 1
        assert ai_ml["keywords"][0]["name"] == "PyTorch"
