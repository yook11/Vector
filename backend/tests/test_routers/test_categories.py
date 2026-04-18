"""/api/v1/categories ルーターエンドポイントのテスト。"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_analysis import ArticleAnalysis
from app.models.category import Category
from app.models.news_source import NewsSource
from app.models.topic import Topic


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

    async def test_article_count(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_categories: list[Category],
        sample_source: NewsSource,
    ) -> None:
        """カテゴリにはトピック経由の記事数が含まれる。"""
        topic = Topic(name="tensorflow", category_id=sample_categories[0].id)
        db_session.add(topic)
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
            topic_id=topic.id,
        )
        db_session.add(analysis)
        await db_session.commit()

        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        ai_cat = next(i for i in items if i["slug"] == "ai")
        assert ai_cat["articleCount"] == 1

    async def test_nested_topics(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_categories: list[Category],
        sample_source: NewsSource,
    ) -> None:
        """カテゴリレスポンスにはネストしたトピック統計が含まれる。"""
        topic = Topic(name="pytorch", category_id=sample_categories[0].id)
        db_session.add(topic)
        await db_session.flush()

        from app.models.news_article import NewsArticle

        article = NewsArticle(
            original_title="PyTorch Article",
            original_url="https://example.com/pytorch",
            news_source_id=sample_source.id,
        )
        db_session.add(article)
        await db_session.flush()

        analysis = ArticleAnalysis(
            news_article_id=article.id,
            translated_title="PyTorch記事",
            summary="要約",
            impact_level="high",
            reasoning="理由",
            ai_model="test",
            topic_id=topic.id,
        )
        db_session.add(analysis)
        await db_session.commit()

        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        ai_cat = next(i for i in items if i["slug"] == "ai")
        assert len(ai_cat["topics"]) == 1
        assert ai_cat["topics"][0]["name"] == "pytorch"
