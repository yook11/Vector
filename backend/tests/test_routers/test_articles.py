"""/api/v1/articles ルーターエンドポイントのテスト。"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource


async def _create_article(
    session: AsyncSession,
    source: NewsSource,
    title: str = "Test AnalyzableArticleRecord",
    url: str = "https://example.com/article",
    published_at: datetime | None = None,
) -> AnalyzableArticleRecord:
    """AnalyzableArticleRecord を作成するヘルパー。"""
    article = AnalyzableArticleRecord(
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
    article: AnalyzableArticleRecord,
    category_id: int,
    translated_title: str = "テスト記事",
    embedding: list[float] | None = None,
    key_points: list[dict] | None = None,
) -> AnalyzedArticleRecord:
    """extraction + analysis を作成するヘルパー。"""
    extraction = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title=translated_title,
        summary="テストの要約",
    )
    session.add(extraction)
    await session.flush()
    analysis = AnalyzedArticleRecord(
        curation_id=extraction.id,
        translated_title=translated_title,
        summary="テストの要約",
        investor_take="Test investor_take",
        embedding=embedding,
        category_id=category_id,
        key_points=key_points,
    )
    session.add(analysis)
    await session.commit()
    await session.refresh(analysis)
    return analysis


@pytest.mark.asyncio
class TestListArticles:
    async def test_requires_bff_proof(self, client: AsyncClient) -> None:
        """BFF 経由証明の無い直叩きは 401 (login 検証ではなく BFF 経由証明)。"""
        resp = await client.get("/api/v1/articles")
        assert resp.status_code == 401

    async def test_empty_list(self, bff_client: AsyncClient) -> None:
        resp = await bff_client.get("/api/v1/articles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["totalPages"] == 0

    async def test_returns_analyzed_articles(
        self,
        bff_client: AsyncClient,
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

        resp = await bff_client.get("/api/v1/articles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    async def test_pagination(
        self,
        bff_client: AsyncClient,
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

        resp = await bff_client.get("/api/v1/articles?page=1&perPage=2")
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
        self, bff_client: AsyncClient, query: str
    ) -> None:
        resp = await bff_client.get(f"/api/v1/articles?{query}")
        assert resp.status_code == 422

    async def test_sort_by_published_at_desc(
        self,
        bff_client: AsyncClient,
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

        resp = await bff_client.get("/api/v1/articles?sortOrder=desc")
        items = resp.json()["items"]
        assert items[0]["translatedTitle"] == "新しい記事"
        assert items[1]["translatedTitle"] == "古い記事"

    async def test_camel_case_response(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        cat_id = sample_categories[0].id
        a = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, a, category_id=cat_id)
        resp = await bff_client.get("/api/v1/articles")
        data = resp.json()
        assert "totalPages" in data
        assert "perPage" in data
        item = data["items"][0]
        assert "translatedTitle" in item
        assert "keyPoints" in item
        assert "summaryPreview" in item
        assert "publishedAt" in item

    async def test_brief_includes_category(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """一覧カードは記事のカテゴリ (slug + name) を含む。"""
        category = sample_categories[0]
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article, category_id=category.id)
        resp = await bff_client.get("/api/v1/articles")
        item = resp.json()["items"][0]
        assert item["category"] == {
            "slug": str(category.slug),
            "name": str(category.name),
        }

    async def test_response_does_not_contain_impact_level(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """API contract: impactLevel must not appear on list items."""
        cat_id = sample_categories[0].id
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article, category_id=cat_id)
        resp = await bff_client.get("/api/v1/articles")
        item = resp.json()["items"][0]
        assert "impactLevel" not in item

    async def test_legacy_impact_level_query_is_ignored(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """Old clients passing ?impactLevel=... must still get a 200."""
        cat_id = sample_categories[0].id
        article = await _create_article(db_session, sample_source)
        await _create_analysis(db_session, article, category_id=cat_id)
        resp = await bff_client.get("/api/v1/articles?impactLevel=high")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_date_sort_tiebreaker_uses_id_desc(
        self,
        bff_client: AsyncClient,
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

        resp = await bff_client.get("/api/v1/articles")
        items = resp.json()["items"]
        assert items[0]["translatedTitle"] == "後の記事"
        assert items[1]["translatedTitle"] == "先の記事"

    @pytest.mark.parametrize("sort_order", ["desc", "asc"])
    async def test_null_published_at_sorts_last(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
        sort_order: str,
    ) -> None:
        """published_at null (日付不明) の記事は並び方向に依らず末尾に来る。

        PostgreSQL の DESC 既定 (NULLS FIRST) のままだと日付不明記事が
        新着の先頭を占有するため NULLS LAST を明示する。除外はしない
        (日付フィルタを持たない一覧契約の維持)。
        """
        cat_id = sample_categories[0].id
        dated = await _create_article(
            db_session,
            sample_source,
            title="Dated",
            url="https://example.com/dated",
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        await _create_analysis(
            db_session, dated, category_id=cat_id, translated_title="日付あり"
        )
        undated = await _create_article(
            db_session,
            sample_source,
            title="Undated",
            url="https://example.com/undated",
        )
        # ヘルパーは published_at 未指定を now に倒すため null は後から剥がす。
        undated.published_at = None
        db_session.add(undated)
        await db_session.commit()
        await _create_analysis(
            db_session, undated, category_id=cat_id, translated_title="日付不明"
        )

        resp = await bff_client.get(f"/api/v1/articles?sortOrder={sort_order}")
        items = resp.json()["items"]
        assert [i["translatedTitle"] for i in items] == ["日付あり", "日付不明"]

    async def test_invalid_category_slug_returns_422(
        self, bff_client: AsyncClient
    ) -> None:
        """CategorySlug VO は slug パターンに合わない値を拒否する。"""
        resp = await bff_client.get("/api/v1/articles?category=INVALID-slug")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["loc"] == ["query", "category"]
        assert "Category slug" in detail[0]["msg"]

    async def test_invalid_category_message_does_not_leak_vo_name(
        self, bff_client: AsyncClient
    ) -> None:
        """422 エラーメッセージに内部 VO クラス名 (CategorySlug) を含めない。"""
        resp = await bff_client.get("/api/v1/articles?category=INVALID-slug")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "CategorySlug" not in detail[0]["msg"]

    async def test_filter_by_category(
        self,
        bff_client: AsyncClient,
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

        resp = await bff_client.get("/api/v1/articles?category=ai")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["translatedTitle"] == "AI 記事"

    async def test_brief_has_summary_preview_key_always_present(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """HTTP response: summaryPreview キーは key_points が非空でも
        常に存在する(null で省略されない)。"""
        article = await _create_article(db_session, sample_source)
        await _create_analysis(
            db_session,
            article,
            category_id=sample_categories[0].id,
            key_points=[{"content": "AIが台頭した。", "mentions": []}],
        )
        resp = await bff_client.get("/api/v1/articles")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert "summaryPreview" in item
        assert item["summaryPreview"] is None

    async def test_brief_does_not_expose_summary_full_text(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """HTTP response: 一覧カードに summary(全文)フィールドが含まれない。"""
        article = await _create_article(db_session, sample_source)
        await _create_analysis(
            db_session,
            article,
            category_id=sample_categories[0].id,
        )
        resp = await bff_client.get("/api/v1/articles")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert "summary" not in item
        assert "keyPoints" in item
        # key_points 空(default fixture)のとき summaryPreview は summary に fallback。
        assert item["summaryPreview"] == "テストの要約"


@pytest.mark.asyncio
class TestGetArticle:
    async def test_get_existing(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session, article, category_id=sample_categories[0].id
        )
        resp = await bff_client.get(f"/api/v1/articles/{analysis.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["translatedTitle"] == "テスト記事"
        assert data["original"]["title"] == "Test AnalyzableArticleRecord"

    async def test_get_nonexistent_returns_404(
        self,
        bff_client: AsyncClient,
    ) -> None:
        resp = await bff_client.get("/api/v1/articles/99999")
        assert resp.status_code == 404

    async def test_get_with_overflowing_id_returns_422(
        self,
        bff_client: AsyncClient,
    ) -> None:
        """INTEGER (int4) 上限超過 ID は asyncpg overflow 前に 422 で弾く。"""
        resp = await bff_client.get("/api/v1/articles/3951638051660537759")
        assert resp.status_code == 422

    async def test_get_with_analysis(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session, article, category_id=sample_categories[0].id
        )

        resp = await bff_client.get(f"/api/v1/articles/{analysis.id}")
        data = resp.json()
        assert data["translatedTitle"] == "テスト記事"
        assert data["investorTake"] == "Test investor_take"
        assert data["original"]["title"] == "Test AnalyzableArticleRecord"
        assert data["original"]["url"] == "https://example.com/article"

    async def test_detail_exposes_key_point_contents(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """詳細は key_points の content だけを ``keyPoints`` に順序保持で出す。"""
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session,
            article,
            category_id=sample_categories[0].id,
            key_points=[
                {
                    "content": "Anthropic が Claude 5 を公開した。",
                    "mentions": [{"surface": "Anthropic", "type": "company"}],
                },
                {"content": "調達額は 10 億ドル。", "mentions": []},
            ],
        )

        resp = await bff_client.get(f"/api/v1/articles/{analysis.id}")
        assert resp.status_code == 200
        assert resp.json()["keyPoints"] == [
            "Anthropic が Claude 5 を公開した。",
            "調達額は 10 億ドル。",
        ]

    async def test_detail_does_not_expose_mentions(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """mentions は trends 内部利用で API 非公開 (surface が JSON 全体に出ない)。"""
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session,
            article,
            category_id=sample_categories[0].id,
            key_points=[
                {
                    "content": "key point body",
                    "mentions": [{"surface": "SecretEntity", "type": "company"}],
                }
            ],
        )

        resp = await bff_client.get(f"/api/v1/articles/{analysis.id}")
        assert resp.status_code == 200
        assert "SecretEntity" not in resp.text

    async def test_detail_null_key_points_returns_empty_list(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """旧行 (key_points IS NULL) は ``keyPoints: []`` で返る (常に存在)。"""
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session,
            article,
            category_id=sample_categories[0].id,
            key_points=None,
        )

        resp = await bff_client.get(f"/api/v1/articles/{analysis.id}")
        assert resp.status_code == 200
        assert resp.json()["keyPoints"] == []

    async def test_detail_includes_category(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """詳細画面は記事のカテゴリ (slug + name) を含む。"""
        category = sample_categories[0]
        expected_slug = str(category.slug)
        expected_name = str(category.name)
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(db_session, article, category_id=category.id)
        analyzed_article_id = analysis.id
        # identity map をクリアし、本番の per-request session と同様に category を
        # クエリの eager load 経由でしか取得できない状態にする。未 eager load だと
        # async の lazy load が MissingGreenlet を投げ 500 になるのを検出する。
        db_session.expunge_all()
        resp = await bff_client.get(f"/api/v1/articles/{analyzed_article_id}")
        assert resp.status_code == 200
        assert resp.json()["category"] == {
            "slug": expected_slug,
            "name": expected_name,
        }


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
    async def test_nonexistent_article_returns_404(
        self, bff_client: AsyncClient
    ) -> None:
        resp = await bff_client.get("/api/v1/articles/99999/similar")
        assert resp.status_code == 404

    async def test_similar_with_overflowing_id_returns_422(
        self, bff_client: AsyncClient
    ) -> None:
        """INTEGER 上限超過 ID は exists_analyzed で overflow 前に 422 で弾く。"""
        resp = await bff_client.get("/api/v1/articles/3951638051660537759/similar")
        assert resp.status_code == 422

    async def test_article_without_embedding_returns_empty_list(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        article = await _create_article(db_session, sample_source)
        analysis = await _create_analysis(
            db_session, article, category_id=sample_categories[0].id
        )

        resp = await bff_client.get(f"/api/v1/articles/{analysis.id}/similar")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_similar_articles_ordered_by_distance(
        self,
        bff_client: AsyncClient,
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

        resp = await bff_client.get(f"/api/v1/articles/{source_analysis.id}/similar")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        assert items[0]["translatedTitle"] == "近い記事"
        assert items[1]["translatedTitle"] == "遠い記事"

    async def test_excludes_source_article(
        self,
        bff_client: AsyncClient,
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

        resp = await bff_client.get(f"/api/v1/articles/{a1_analysis.id}/similar")
        items = resp.json()
        returned_ids = [item["id"] for item in items]
        assert a1_analysis.id not in returned_ids
        assert a2_analysis.id in returned_ids

    async def test_respects_limit_parameter(
        self,
        bff_client: AsyncClient,
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

        resp = await bff_client.get(
            f"/api/v1/articles/{source_analysis.id}/similar", params={"limit": 2}
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_similar_carries_new_brief_contract(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        # 類似記事も build_brief を共有する。素の配列 envelope が新 brief 契約
        # (keyPoints 搭載 / summary 全文不在 / 相互排他) を載せることを確認する。
        cat_id = sample_categories[0].id
        source = await _create_article(
            db_session, sample_source, url="https://example.com/src-brief"
        )
        source_analysis = await _create_analysis(
            db_session, source, category_id=cat_id, embedding=EMBEDDING_A
        )
        similar = await _create_article(
            db_session, sample_source, url="https://example.com/similar-brief"
        )
        await _create_analysis(
            db_session,
            similar,
            category_id=cat_id,
            embedding=EMBEDDING_B,
            key_points=[{"content": "AIが台頭した。", "mentions": []}],
        )

        resp = await bff_client.get(f"/api/v1/articles/{source_analysis.id}/similar")
        assert resp.status_code == 200
        item = resp.json()[0]
        assert item["keyPoints"] == ["AIが台頭した。"]
        assert item["summaryPreview"] is None
        assert "summary" not in item
