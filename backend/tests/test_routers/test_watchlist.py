"""/api/v1/me/watchlist ルーターエンドポイントのテスト。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx2 import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource
from app.models.watchlist_entry import WatchlistEntry
from app.schemas.articles import ArticleListPosition
from app.schemas.cursor import encode_cursor
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID


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


async def _watch_new_articles(
    db_session: AsyncSession,
    source: NewsSource,
    category_id: int,
    watched_at: list[datetime],
    *,
    user_id: str = TEST_USER_ID,
) -> list[int]:
    """ウォッチ時刻ごとに分析済み記事を作ってウォッチし、作った順の記事 ID を返す。"""
    ids = []
    for at in watched_at:
        _, analysis = await _build_article_with_analysis(
            db_session,
            source,
            category_id,
            url=f"https://example.com/{uuid4()}",
            title="Watched",
            translated_title="ウォッチ記事",
            summary="要約",
            investor_take="見解",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(
            WatchlistEntry(
                user_id=UUID(user_id), analyzed_article_id=analysis.id, created_at=at
            )
        )
        ids.append(analysis.id)
    await db_session.commit()
    return ids


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
        """ウォッチが無いと、空の一覧と「続きなし」が返る。"""
        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "nextCursor": None}

    async def test_returns_watchlist_items(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
        sample_categories: list[Category],
    ) -> None:
        await authed_client.put(f"/api/v1/me/watchlist/{sample_article.id}")

        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["nextCursor"] is None
        [item] = data["items"]
        assert item["id"] == sample_article.id
        assert item["translatedTitle"] == "テスト記事"
        # AnalyzedArticlePreview 契約: summary 全文は返さず
        # keyPoints / summaryPreview を返す。
        # fixture は key_points 未指定 (空) のため summaryPreview にフォールバック。
        assert "summary" not in item
        assert item["keyPoints"] == []
        assert item["summaryPreview"] == "テストの要約"
        assert item["source"]["name"] == "Test Tech Source"
        # watchlist 経路も brief の eager load を共有し category を返す
        assert item["category"]["slug"] == str(sample_categories[0].slug)
        # Pattern B: AnalyzedArticlePreview から isWatched は削除済み
        assert "isWatched" not in item

    async def test_continues_from_cursor_in_watch_order(
        self,
        authed_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """25件ウォッチすると新しく入れた順に24件と続きが返り、続きで残りの1件が返る。"""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        ids = await _watch_new_articles(
            db_session,
            sample_source,
            sample_categories[0].id,
            [start + timedelta(minutes=i) for i in range(25)],
        )
        newest_first = ids[::-1]

        first = (await authed_client.get("/api/v1/me/watchlist")).json()
        rest = (
            await authed_client.get(
                "/api/v1/me/watchlist", params={"cursor": first["nextCursor"]}
            )
        ).json()

        assert [item["id"] for item in first["items"]] == newest_first[:24]
        assert first["nextCursor"] is not None
        assert [item["id"] for item in rest["items"]] == newest_first[24:]
        assert rest["nextCursor"] is None

    async def test_same_watch_time_continues_without_gap_or_duplicate(
        self,
        authed_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """ウォッチ時刻が同じ記事が境目をまたいでも、続きは重複も欠けもなく返る。"""
        same_time = datetime(2026, 1, 1, tzinfo=UTC)
        ids = await _watch_new_articles(
            db_session, sample_source, sample_categories[0].id, [same_time] * 26
        )

        first = (await authed_client.get("/api/v1/me/watchlist")).json()
        rest = (
            await authed_client.get(
                "/api/v1/me/watchlist", params={"cursor": first["nextCursor"]}
            )
        ).json()

        returned = [item["id"] for item in first["items"] + rest["items"]]
        assert returned == sorted(ids, reverse=True)
        assert rest["nextCursor"] is None

    async def test_other_users_watches_are_not_returned(
        self,
        authed_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """他のユーザーがウォッチした記事は、自分のウォッチリストに入らない。"""
        at = datetime(2026, 1, 1, tzinfo=UTC)
        mine = await _watch_new_articles(
            db_session, sample_source, sample_categories[0].id, [at]
        )
        await _watch_new_articles(
            db_session,
            sample_source,
            sample_categories[0].id,
            [at + timedelta(minutes=1)],
            user_id=TEST_ADMIN_ID,
        )

        resp = await authed_client.get("/api/v1/me/watchlist")

        assert [item["id"] for item in resp.json()["items"]] == mine

    @pytest.mark.parametrize(
        "cursor",
        [
            pytest.param("not base64!", id="base64 の文字以外を含む"),
            pytest.param(
                encode_cursor(
                    ArticleListPosition(
                        published_at=datetime(2026, 1, 1, tzinfo=UTC), id=1
                    )
                ),
                id="記事一覧のカーソル",
            ),
        ],
    )
    async def test_unreadable_cursor_returns_422(
        self, authed_client: AsyncClient, cursor: str
    ) -> None:
        """読めないカーソルは DB に問い合わせる前に 422 で弾く。"""
        resp = await authed_client.get(
            "/api/v1/me/watchlist", params={"cursor": cursor}
        )
        assert resp.status_code == 422
        [error] = resp.json()["detail"]
        assert error["loc"] == ["query", "cursor"]

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
        """未登録の記事を PUT すると 201 で、ウォッチリストに入る。"""
        resp = await authed_client.put(f"/api/v1/me/watchlist/{sample_article.id}")
        assert resp.status_code == 201

        resp = await authed_client.get("/api/v1/me/watchlist/ids")
        assert resp.json() == {"ids": [sample_article.id]}

    async def test_add_already_watched_returns_204(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
    ) -> None:
        """登録済みの記事をもう一度 PUT しても 204 で、ウォッチは1件のまま。"""
        await authed_client.put(f"/api/v1/me/watchlist/{sample_article.id}")
        resp = await authed_client.put(f"/api/v1/me/watchlist/{sample_article.id}")
        assert resp.status_code == 204

        resp = await authed_client.get("/api/v1/me/watchlist/ids")
        assert resp.json() == {"ids": [sample_article.id]}

    async def test_add_nonexistent_article_404(
        self, authed_client: AsyncClient
    ) -> None:
        resp = await authed_client.put("/api/v1/me/watchlist/99999")
        assert resp.status_code == 404

    async def test_add_with_overflowing_id_returns_422(
        self, authed_client: AsyncClient
    ) -> None:
        """integer (int4) の上限 + 1 の ID は、DB に問い合わせる前に 422 で弾く。"""
        resp = await authed_client.put("/api/v1/me/watchlist/2147483648")
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestRemoveFromWatchlist:
    async def test_remove_success(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
    ) -> None:
        await authed_client.put(f"/api/v1/me/watchlist/{sample_article.id}")
        resp = await authed_client.delete(f"/api/v1/me/watchlist/{sample_article.id}")
        assert resp.status_code == 204

        # 削除されたことを確認
        resp = await authed_client.get("/api/v1/me/watchlist")
        assert resp.json()["items"] == []

    async def test_remove_not_watched_returns_204(
        self, authed_client: AsyncClient
    ) -> None:
        """登録されていない記事を DELETE しても 204 を返す。"""
        resp = await authed_client.delete("/api/v1/me/watchlist/99999")
        assert resp.status_code == 204

    async def test_remove_with_overflowing_id_returns_422(
        self, authed_client: AsyncClient
    ) -> None:
        """integer (int4) の上限 + 1 の ID は、DB に問い合わせる前に 422 で弾く。"""
        resp = await authed_client.delete("/api/v1/me/watchlist/2147483648")
        assert resp.status_code == 422


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
        await authed_client.put(f"/api/v1/me/watchlist/{sample_article.id}")
        await authed_client.put(f"/api/v1/me/watchlist/{second_article.id}")

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
        await authed_client.put(f"/api/v1/me/watchlist/{sample_article.id}")

        resp = await authed_client.get("/api/v1/articles")
        items = resp.json()["items"]
        assert len(items) == 1
        assert "isWatched" not in items[0]

    async def test_article_detail_does_not_include_is_watched(
        self,
        authed_client: AsyncClient,
        sample_article: AnalyzedArticleRecord,
    ) -> None:
        await authed_client.put(f"/api/v1/me/watchlist/{sample_article.id}")

        resp = await authed_client.get(f"/api/v1/articles/{sample_article.id}")
        assert resp.status_code == 200
        assert "isWatched" not in resp.json()
