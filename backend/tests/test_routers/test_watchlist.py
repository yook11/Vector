"""/api/v1/me/watchlist ルーターエンドポイントのテスト。"""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource


async def _build_article_with_analysis(
    db_session: AsyncSession,
    source: NewsSource,
    category_id: int,
    *,
    url: str,
    title: str,
    translated_title: str,
    summary: str,
    investor_take: str,
    published_at: datetime,
) -> tuple[AnalyzableArticleRecord, AnalyzedArticleRecord]:
    article = AnalyzableArticleRecord(
        source_id=source.id,
        source_url=url,
        original_title=title,
        original_content="content",
        published_at=published_at,
    )
    db_session.add(article)
    await db_session.flush()
    extraction = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title=translated_title,
        summary=summary,
    )
    db_session.add(extraction)
    await db_session.flush()
    analysis = AnalyzedArticleRecord(
        curation_id=extraction.id,
        translated_title=translated_title,
        summary=summary,
        investor_take=investor_take,
        category_id=category_id,
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)
    await db_session.refresh(article, ["curation"])
    return article, analysis


@pytest.fixture
async def sample_article(
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> AnalyzedArticleRecord:
    """分析付きのテスト用記事（analysis を返す）。"""
    _, analysis = await _build_article_with_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/test",
        title="Test AnalyzableArticleRecord",
        translated_title="テスト記事",
        summary="テストの要約",
        investor_take="Test investor_take",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return analysis


@pytest.fixture
async def second_article(
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> AnalyzedArticleRecord:
    """分析付きの 2 件目のテスト用記事（analysis を返す）。"""
    _, analysis = await _build_article_with_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/second",
        title="Second AnalyzableArticleRecord",
        translated_title="2番目の記事",
        summary="2番目の要約",
        investor_take="Second investor_take",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    return analysis


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
        sample_article: AnalyzedArticleRecord,
        sample_categories: list[Category],
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )

        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["id"] == sample_article.id
        assert item["translatedTitle"] == "テスト記事"
        # ArticleBrief 契約: summary 全文は返さず keyPoints / summaryPreview を返す。
        # fixture は key_points 未指定 (空) のため summaryPreview にフォールバック。
        assert "summary" not in item
        assert item["keyPoints"] == []
        assert item["summaryPreview"] == "テストの要約"
        assert item["source"]["name"] == "Test Tech Source"
        # watchlist 経路も brief の eager load を共有し category を返す
        assert item["category"]["slug"] == str(sample_categories[0].slug)
        # Pattern B: ArticleBrief から isWatched は削除済み
        assert "isWatched" not in item

    async def test_pagination(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
        second_article: AnalyzedArticleRecord,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": second_article.id},
        )

        resp = await authed_client.get("/api/v1/me/watchlist?perPage=1&page=1")
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 1
        assert data["totalPages"] == 2

    async def test_missing_auth_headers(self, client: AsyncClient) -> None:
        """Authorization ヘッダーが無い場合は 401 (BFF JWT 未提示)。"""
        resp = await client.get("/api/v1/me/watchlist")
        assert resp.status_code == 401

    async def test_bff_proof_without_user_rejected(
        self, bff_client: AsyncClient
    ) -> None:
        """BFF 経由証明だけ (sub/role 無し) では user endpoint は 401。

        共有 read は通るが watchlist は user identity を要求する非対称を固定する。
        """
        resp = await bff_client.get("/api/v1/me/watchlist")
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestAddToWatchlist:
    async def test_add_success(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
    ) -> None:
        resp = await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )
        assert resp.status_code == 201

    async def test_add_duplicate_409(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )
        resp = await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )
        assert resp.status_code == 409
        # red-team chain θ-1: detail は allowlist 通過 form で固定。
        assert resp.json() == {"detail": "Watchlist entry already exists"}

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
        sample_article: AnalyzedArticleRecord,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )
        resp = await authed_client.delete(f"/api/v1/me/watchlist/{sample_article.id}")
        assert resp.status_code == 204

        # 削除されたことを確認
        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.json()["total"] == 0

    async def test_remove_not_found(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.delete("/api/v1/me/watchlist/99999")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestListWatchlistIds:
    async def test_empty_returns_empty_ids(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.get("/api/v1/me/watchlist/ids")
        assert resp.status_code == 200
        assert resp.json() == {"ids": []}

    async def test_returns_ids_newest_first(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
        second_article: AnalyzedArticleRecord,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": second_article.id},
        )

        resp = await authed_client.get("/api/v1/me/watchlist/ids")
        assert resp.status_code == 200
        # 後に追加した second_article が先頭
        assert resp.json() == {"ids": [second_article.id, sample_article.id]}

    async def test_unauthenticated_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/me/watchlist/ids")
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestArticlesNoIsWatched:
    async def test_articles_list_does_not_include_is_watched(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
    ) -> None:
        """Pattern B: per-user フラグは記事スキーマに含まない (cache 安全のため)。"""
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )

        resp = await authed_client.get("/api/v1/articles")
        items = resp.json()["items"]
        assert len(items) == 1
        assert "isWatched" not in items[0]

    async def test_article_detail_does_not_include_is_watched(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
    ) -> None:
        await authed_client.post(
            "/api/v1/me/watchlist",
            json={"articleId": sample_article.id},
        )

        resp = await authed_client.get(f"/api/v1/articles/{sample_article.id}")
        assert resp.status_code == 200
        assert "isWatched" not in resp.json()
