"""/api/v1/articles ルーターエンドポイントのテスト。"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource


async def _create_article(
    session: AsyncSession,
    source: NewsSource,
    title: str = "Test Article",
    url: str = "https://example.com/article",
    published_at: datetime | None = None,
) -> Article:
    """Article を作成するヘルパー。"""
    article = Article(
        source_id=source.id,
        source_url=url,
        original_title=title,
        original_content="Test content.",
        published_at=published_at or datetime.now(UTC),
    )
    session.add(article)
    await session.commit()
    await session.refresh(article)
    return article


async def _create_analysis(
    session: AsyncSession,
    article: Article,
    category_id: int,
    translated_title: str = "テスト記事",
    embedding: list[float] | None = None,
) -> InScopeAssessment:
    """extraction + analysis を作成するヘルパー。"""
    extraction = ArticleCuration(
        article_id=article.id,
        translated_title=translated_title,
        summary="テストの要約",
    )
    session.add(extraction)
    await session.flush()
    analysis = InScopeAssessment(
        curation_id=extraction.id,
        translated_title=translated_title,
        summary="テストの要約",
        investor_take="Test investor_take",
        embedding=embedding,
        category_id=category_id,
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
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        cat_id = sample_categories[0].id
        a1 = await _create_article(
            db_session, sample_source, url="https://example.com/1"
        )
        await _create_analysis(db_session, a1, category_id=cat_id)
        a2 = await _create_article(
            db_session, sample_source, url="https://example.com/2"
        )
        await _create_analysis(db_session, a2, category_id=cat_id)
        # 未分析の記事は除外されるはず
        await _create_article(db_session, sample_source, url="https://example.com/3")

        resp = await client.get("/api/v1/articles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    async def test_pagination(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        cat_id = sample_categories[0].id
        for i in range(5):
            article = await _create_article(
                db_session, sample_source, url=f"https://example.com/{i}"
            )
            await _create_analysis(db_session, article, category_id=cat_id)

        resp = await client.get("/api/v1/articles?page=1&perPage=2")
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["page"] == 1
        assert data["perPage"] == 2
        assert data["totalPages"] == 3

    @pytest.mark.parametrize(
        "query",
        [
            "page=0",
            "page=10001",
            "perPage=0",
            "perPage=101",
        ],
    )
    async def test_invalid_pagination_returns_422(
        self, client: AsyncClient, query: str
    ) -> None:
        resp = await client.get(f"/api/v1/articles?{query}")
        assert resp.status_code == 422

    async def test_sort_by_published_at_desc(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        cat_id = sample_categories[0].id
        now = datetime.now(UTC)
        older = await _create_article(
            db_session,
            sample_source,
            title="Older",
            url="https://example.com/old",
            published_at=now - timedelta(days=2),
        )
        await _create_analysis(
            db_session, older, category_id=cat_id, translated_title="古い記事"
        )
        newer = await _create_article(
            db_session,
            sample_source,
            title="Newer",
            url="https://example.com/new",
            published_at=now,
        )
        await _create_analysis(
            db_session, newer, category_id=cat_id, translated_title="新しい記事"
        )

        resp = await client.get("/api/v1/articles?sortOrder=desc")
        items = resp.json()["items"]
        assert items[0]["translatedTitle"] == "新しい記事"
        assert items[1]["translatedTitle"] == "古い記事"

    async def test_camel_case_response(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        cat_id = sample_categories[0].id
        a = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, a, category_id=cat_id)
        resp = await client.get("/api/v1/articles")
        data = resp.json()
        assert "totalPages" in data
        assert "perPage" in data
        item = data["items"][0]
        assert "translatedTitle" in item
        assert "summary" in item
        assert "publishedAt" in item

    async def test_brief_includes_category(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """一覧カードは記事のカテゴリ (slug + name) を含む。"""
        category = sample_categories[0]
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article, category_id=category.id)
        resp = await client.get("/api/v1/articles")
        item = resp.json()["items"][0]
        assert item["category"] == {
            "slug": str(category.slug),
            "name": str(category.name),
        }

    async def test_response_does_not_contain_impact_level(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """API contract: impactLevel must not appear on list items."""
        cat_id = sample_categories[0].id
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article, category_id=cat_id)
        resp = await client.get("/api/v1/articles")
        item = resp.json()["items"][0]
        assert "impactLevel" not in item

    async def test_legacy_impact_level_query_is_ignored(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """Old clients passing ?impactLevel=... must still get a 200."""
        cat_id = sample_categories[0].id
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article, category_id=cat_id)
        resp = await client.get("/api/v1/articles?impactLevel=high")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_date_sort_tiebreaker_uses_id_desc(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """published_at が同一の場合は id DESC で並び替える。"""
        cat_id = sample_categories[0].id
        same_time = datetime(2025, 1, 1, tzinfo=UTC)
        a1 = await _create_article(
            db_session,
            sample_source,
            title="First",
            url="https://example.com/tie1",
            published_at=same_time,
        )
        await _create_analysis(
            db_session, a1, category_id=cat_id, translated_title="先の記事"
        )
        a2 = await _create_article(
            db_session,
            sample_source,
            title="Second",
            url="https://example.com/tie2",
            published_at=same_time,
        )
        await _create_analysis(
            db_session, a2, category_id=cat_id, translated_title="後の記事"
        )

        resp = await client.get("/api/v1/articles")
        items = resp.json()["items"]
        assert items[0]["translatedTitle"] == "後の記事"
        assert items[1]["translatedTitle"] == "先の記事"

    async def test_invalid_category_slug_returns_422(self, client: AsyncClient) -> None:
        """CategorySlug VO は slug パターンに合わない値を拒否する。"""
        resp = await client.get("/api/v1/articles?category=INVALID-slug")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["loc"] == ["query", "category"]
        assert "Category slug" in detail[0]["msg"]

    async def test_invalid_category_message_does_not_leak_vo_name(
        self, client: AsyncClient
    ) -> None:
        """422 エラーメッセージに内部 VO クラス名 (CategorySlug) を含めない。"""
        resp = await client.get("/api/v1/articles?category=INVALID-slug")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "CategorySlug" not in detail[0]["msg"]

    async def test_filter_by_category(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """category パラメータは指定スラッグの category_id を持つ記事のみ返す。"""
        target = await _create_article(
            db_session, sample_source, url="https://example.com/ai"
        )
        await _create_analysis(
            db_session,
            target,
            category_id=sample_categories[0].id,
            translated_title="AI 記事",
        )
        other = await _create_article(
            db_session, sample_source, url="https://example.com/qc"
        )
        await _create_analysis(
            db_session,
            other,
            category_id=sample_categories[1].id,
            translated_title="量子記事",
        )

        resp = await client.get("/api/v1/articles?category=ai")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["translatedTitle"] == "AI 記事"


@pytest.mark.asyncio
class TestGetArticle:
    async def test_get_existing(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session, article, category_id=sample_categories[0].id
        )
        resp = await client.get(f"/api/v1/articles/{analysis.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["translatedTitle"] == "テスト記事"
        assert data["original"]["title"] == "Test Article"

    async def test_get_nonexistent_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.get("/api/v1/articles/99999")
        assert resp.status_code == 404

    async def test_get_with_overflowing_id_returns_422(
        self,
        client: AsyncClient,
    ) -> None:
        """INTEGER (int4) 上限超過 ID は asyncpg overflow 前に 422 で弾く。"""
        resp = await client.get("/api/v1/articles/3951638051660537759")
        assert resp.status_code == 422

    async def test_get_with_analysis(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session, article, category_id=sample_categories[0].id
        )

        resp = await client.get(f"/api/v1/articles/{analysis.id}")
        data = resp.json()
        assert data["translatedTitle"] == "テスト記事"
        assert data["investorTake"] == "Test investor_take"
        assert data["original"]["title"] == "Test Article"
        assert data["original"]["url"] == "https://example.com/article"


# 次元はモデルの Vector(768) と一致させる必要がある
_DIM = 768


def _make_embedding(base: float) -> list[float]:
    """定数値で埋めて正規化した 768 次元 embedding を作成する。"""
    vec = [base] * _DIM
    norm = (base**2 * _DIM) ** 0.5
    return [v / norm for v in vec]


# 近い embedding 2 つと遠い embedding 1 つ
EMBEDDING_A = _make_embedding(1.0)
EMBEDDING_B = _make_embedding(0.95)
EMBEDDING_FAR = _make_embedding(-1.0)


@pytest.mark.asyncio
class TestSimilarArticles:
    async def test_nonexistent_article_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/articles/99999/similar")
        assert resp.status_code == 404

    async def test_similar_with_overflowing_id_returns_422(
        self, client: AsyncClient
    ) -> None:
        """INTEGER 上限超過 ID は exists_analyzed で overflow 前に 422 で弾く。"""
        resp = await client.get("/api/v1/articles/3951638051660537759/similar")
        assert resp.status_code == 422

    async def test_article_without_embedding_returns_empty_list(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session, article, category_id=sample_categories[0].id
        )

        resp = await client.get(f"/api/v1/articles/{analysis.id}/similar")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_similar_articles_ordered_by_distance(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        cat_id = sample_categories[0].id
        source = await _create_article(
            db_session, sample_source, url="https://example.com/src"
        )
        source_analysis = await _create_analysis(
            db_session, source, category_id=cat_id, embedding=EMBEDDING_A
        )

        close = await _create_article(
            db_session, sample_source, url="https://example.com/close"
        )
        await _create_analysis(
            db_session,
            close,
            category_id=cat_id,
            translated_title="近い記事",
            embedding=EMBEDDING_B,
        )

        far = await _create_article(
            db_session, sample_source, url="https://example.com/far"
        )
        await _create_analysis(
            db_session,
            far,
            category_id=cat_id,
            translated_title="遠い記事",
            embedding=EMBEDDING_FAR,
        )

        resp = await client.get(f"/api/v1/articles/{source_analysis.id}/similar")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        assert items[0]["translatedTitle"] == "近い記事"
        assert items[1]["translatedTitle"] == "遠い記事"

    async def test_excludes_source_article(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        cat_id = sample_categories[0].id
        a1 = await _create_article(
            db_session, sample_source, url="https://example.com/a1"
        )
        a1_analysis = await _create_analysis(
            db_session, a1, category_id=cat_id, embedding=EMBEDDING_A
        )

        a2 = await _create_article(
            db_session, sample_source, url="https://example.com/a2"
        )
        a2_analysis = await _create_analysis(
            db_session, a2, category_id=cat_id, embedding=EMBEDDING_A
        )

        resp = await client.get(f"/api/v1/articles/{a1_analysis.id}/similar")
        items = resp.json()
        returned_ids = [item["id"] for item in items]
        assert a1_analysis.id not in returned_ids
        assert a2_analysis.id in returned_ids

    async def test_respects_limit_parameter(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        cat_id = sample_categories[0].id
        source = await _create_article(
            db_session, sample_source, url="https://example.com/main"
        )
        source_analysis = await _create_analysis(
            db_session, source, category_id=cat_id, embedding=EMBEDDING_A
        )

        for i in range(5):
            art = await _create_article(
                db_session, sample_source, url=f"https://example.com/s{i}"
            )
            await _create_analysis(
                db_session, art, category_id=cat_id, embedding=EMBEDDING_B
            )

        resp = await client.get(
            f"/api/v1/articles/{source_analysis.id}/similar", params={"limit": 2}
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2
