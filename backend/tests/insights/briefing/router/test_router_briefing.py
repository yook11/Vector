"""GET /api/v1/briefing/{categorySlug} のエンドポイントテスト。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.domain.briefing import (
    MAX_CHAPTER_BODY_LEN,
    MAX_CHAPTERS_PER_BRIEFING,
    MAX_KEY_ARTICLE_SIGNIFICANCE_LEN,
    MAX_KEY_ARTICLES_PER_BRIEFING,
    MAX_WATCH_POINT_STATEMENT_LEN,
    MAX_WATCH_POINTS_PER_BRIEFING,
)
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.weekly_briefing import WeeklyBriefing

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
async def ai_category(db_session: AsyncSession) -> Category:
    cat = Category(slug="ai", name="AI")
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)
    return cat


class TestGetBriefing:
    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_category(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/briefing/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_slug",
        [
            "AI",  # 大文字混入
            "ai-ml",  # ハイフン (slug は underscore 区切り)
            "_ai",  # 先頭 underscore
            "a" * 51,  # 長過ぎ
            "%E2%80%A8",  # 異常 UTF-8 (Schemathesis Finding #3 の reproducer 系)
        ],
    )
    async def test_returns_422_for_invalid_slug_pattern(
        self, client: AsyncClient, bad_slug: str
    ) -> None:
        """Path pattern 違反は 404 (DB 検索) ではなく 422 (schema reject) で弾く。"""
        resp = await client.get(f"/api/v1/briefing/{bad_slug}")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_briefing(
        self, client: AsyncClient, ai_category: Category
    ) -> None:
        resp = await client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "empty"
        assert body["category"]["slug"] == "ai"

    @pytest.mark.asyncio
    async def test_returns_ready_with_articles(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        published_at = datetime(2026, 4, 21, 9, 0, tzinfo=JST)
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="記事タイトル",
            published_at=published_at,
        )
        # extraction relation を lazy load しないために article_id を SQL で取得
        result = await db_session.execute(
            select(ArticleCuration.article_id).where(
                ArticleCuration.id == analysis.curation_id
            )
        )
        article_id = result.scalar_one()

        briefing = WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="今週のヘッドライン",
            summary="今週の総括リード",
            chapters=[{"heading": "資金とインフラ", "body": "今週の流れの本文"}],
            key_articles=[{"article_id": article_id, "significance": "なぜ重要か"}],
            watch_points=[{"statement": "今後どこを見るべきか"}],
            model_name="deepseek-v4-pro",
            input_article_count=1,
        )
        db_session.add(briefing)
        await db_session.commit()

        resp = await client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "ready"
        assert body["category"]["slug"] == "ai"
        assert body["headline"] == "今週のヘッドライン"
        assert body["summary"] == "今週の総括リード"
        assert body["chapters"] == [
            {"heading": "資金とインフラ", "body": "今週の流れの本文"}
        ]
        assert body["modelName"] == "deepseek-v4-pro"
        assert body["inputArticleCount"] == 1
        assert len(body["keyArticles"]) == 1
        assert body["keyArticles"][0]["articleId"] == article_id
        assert body["keyArticles"][0]["significance"] == "なぜ重要か"
        assert len(body["watchPoints"]) == 1
        assert body["watchPoints"][0]["statement"] == "今後どこを見るべきか"
        assert len(body["articles"]) == 1
        assert body["articles"][0]["id"] == article_id
        assert body["articles"][0]["titleJa"] == "記事タイトル"
        assert (
            datetime.fromisoformat(body["articles"][0]["publishedAt"]) == published_at
        )

    @pytest.mark.asyncio
    async def test_article_published_at_is_null_when_unset(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """``Article.published_at`` 未設定の記事は ``publishedAt: null`` で返る。"""
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        result = await db_session.execute(
            select(ArticleCuration.article_id).where(
                ArticleCuration.id == analysis.curation_id
            )
        )
        article_id = result.scalar_one()

        briefing = WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="今週のヘッドライン",
            summary="今週の総括リード",
            chapters=[{"heading": "資金とインフラ", "body": "今週の流れの本文"}],
            key_articles=[{"article_id": article_id, "significance": "なぜ重要か"}],
            watch_points=[{"statement": "今後どこを見るべきか"}],
            model_name="deepseek-v4-pro",
            input_article_count=1,
        )
        db_session.add(briefing)
        await db_session.commit()

        resp = await client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        body = resp.json()
        assert body["articles"][0]["publishedAt"] is None


class TestListBriefings:
    @pytest.mark.asyncio
    async def test_returns_all_categories_with_latest_field(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """未生成カテゴリは latest=None で 11 行 (本テストでは 2 カテゴリ) 揃う。"""
        # 別カテゴリも追加
        other = Category(slug="robotics", name="ロボティクス")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)

        resp = await client.get("/api/v1/briefing")
        assert resp.status_code == 200
        body = resp.json()
        assert "currentWeekStart" in body
        slugs = [item["category"]["slug"] for item in body["items"]]
        assert "ai" in slugs and "robotics" in slugs
        # どちらも未生成なので latest は None
        for item in body["items"]:
            assert item["latest"] is None

    @pytest.mark.asyncio
    async def test_includes_headline_for_ready_item(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        briefing = WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="今週のヘッドライン",
            summary="今週の総括リード",
            chapters=[{"heading": "資金とインフラ", "body": "今週の流れの本文"}],
            key_articles=[{"article_id": 1, "significance": "なぜ重要か"}],
            watch_points=[{"statement": "今後どこを見るべきか"}],
            model_name="deepseek-v4-pro",
            input_article_count=1,
        )
        db_session.add(briefing)
        await db_session.commit()

        resp = await client.get("/api/v1/briefing")
        assert resp.status_code == 200
        body = resp.json()
        ai_item = next(i for i in body["items"] if i["category"]["slug"] == "ai")
        assert ai_item["latest"] is not None
        assert ai_item["latest"]["weekStart"] == "2026-04-20"
        # 一覧は短い headline をそのまま返す (旧 headlineExcerpt 抜粋ロジックは廃止)
        assert ai_item["latest"]["headline"] == "今週のヘッドライン"

    @pytest.mark.asyncio
    async def test_orders_items_by_category_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        # ai は最初に登録されているので id が小さい想定
        b_cat = Category(slug="bio", name="バイオ")
        db_session.add(b_cat)
        await db_session.commit()
        await db_session.refresh(b_cat)

        resp = await client.get("/api/v1/briefing")
        body = resp.json()
        ids = [item["category"]["id"] for item in body["items"]]
        assert ids == sorted(ids)


class TestBriefingResponseSizeGuard:
    """red-team F10: anon GET 経路で巨大 briefing JSONB が response として
    流れる経路を構造的に塞ぐ。

    AUTH-N4 / AUTH-C1 経由で attacker が DB に巨大 key_articles / watch_points を
    直書きしたシナリオ。Field(max_length=...) が router の
    `_KeyArticleOut`/`_WatchPointOut` の model_validate または `ReadyBriefing(...)`
    構築時に発火し、response に巨大 JSONB が含まれることを構造的に防ぐ。
    """

    def _persist(
        self,
        db_session: AsyncSession,
        ai_category: Category,
        *,
        key_articles: list[dict],
        watch_points: list[dict],
        chapters: list[dict] | None = None,
    ) -> WeeklyBriefing:
        return WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="h",
            summary="s",
            chapters=chapters or [{"heading": "h", "body": "b"}],
            key_articles=key_articles,
            watch_points=watch_points,
            model_name="deepseek-v4-pro",
            input_article_count=1,
        )

    @pytest.mark.asyncio
    async def test_oversize_key_articles_rejected_by_response_model(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """key_articles 数が上限超なら anon GET で ValidationError 伝播 (本番 500)。"""
        oversized = [
            {"article_id": i, "significance": f"s{i}"}
            for i in range(MAX_KEY_ARTICLES_PER_BRIEFING + 1)
        ]
        db_session.add(
            self._persist(
                db_session,
                ai_category,
                key_articles=oversized,
                watch_points=[{"statement": "w"}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/briefing/ai")

    @pytest.mark.asyncio
    async def test_oversize_significance_rejected_by_response_model(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """1 件の significance が上限超なら anon GET で ValidationError 伝播。"""
        db_session.add(
            self._persist(
                db_session,
                ai_category,
                key_articles=[
                    {
                        "article_id": 1,
                        "significance": "x" * (MAX_KEY_ARTICLE_SIGNIFICANCE_LEN + 1),
                    }
                ],
                watch_points=[{"statement": "w"}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/briefing/ai")

    @pytest.mark.asyncio
    async def test_oversize_watch_points_rejected_by_response_model(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """watch_points 数が上限超なら anon GET で ValidationError 伝播。"""
        oversized = [
            {"statement": f"w{i}"} for i in range(MAX_WATCH_POINTS_PER_BRIEFING + 1)
        ]
        db_session.add(
            self._persist(
                db_session,
                ai_category,
                key_articles=[{"article_id": 1, "significance": "s"}],
                watch_points=oversized,
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/briefing/ai")

    @pytest.mark.asyncio
    async def test_oversize_statement_rejected_by_response_model(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """1 件の statement が上限超なら anon GET で ValidationError 伝播。"""
        db_session.add(
            self._persist(
                db_session,
                ai_category,
                key_articles=[{"article_id": 1, "significance": "s"}],
                watch_points=[{"statement": "x" * (MAX_WATCH_POINT_STATEMENT_LEN + 1)}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/briefing/ai")

    @pytest.mark.asyncio
    async def test_oversize_chapters_rejected_by_response_model(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """chapters 数が上限超なら anon GET で ValidationError 伝播 (本番 500)。"""
        oversized = [
            {"heading": f"h{i}", "body": f"b{i}"}
            for i in range(MAX_CHAPTERS_PER_BRIEFING + 1)
        ]
        db_session.add(
            self._persist(
                db_session,
                ai_category,
                key_articles=[{"article_id": 1, "significance": "s"}],
                watch_points=[{"statement": "w"}],
                chapters=oversized,
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/briefing/ai")

    @pytest.mark.asyncio
    async def test_oversize_chapter_body_rejected_by_response_model(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """1 章の body が上限超なら anon GET で ValidationError 伝播。"""
        db_session.add(
            self._persist(
                db_session,
                ai_category,
                key_articles=[{"article_id": 1, "significance": "s"}],
                watch_points=[{"statement": "w"}],
                chapters=[{"heading": "h", "body": "x" * (MAX_CHAPTER_BODY_LEN + 1)}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError):
            await client.get("/api/v1/briefing/ai")
