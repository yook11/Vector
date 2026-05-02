"""GET /api/v1/briefing/{categorySlug} のエンドポイントテスト。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_extraction import ArticleExtraction
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
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="記事タイトル",
        )
        # extraction relation を lazy load しないために article_id を SQL で取得
        result = await db_session.execute(
            select(ArticleExtraction.article_id).where(
                ArticleExtraction.id == analysis.extraction_id
            )
        )
        article_id = result.scalar_one()

        briefing = WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="今週のヘッドライン",
            stories=[
                {
                    "title": "ストーリーA",
                    "analysis": "分析本文",
                    "article_ids": [article_id],
                }
            ],
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
        assert body["modelName"] == "deepseek-v4-pro"
        assert body["inputArticleCount"] == 1
        assert len(body["stories"]) == 1
        assert body["stories"][0]["articleIds"] == [article_id]
        assert len(body["articles"]) == 1
        assert body["articles"][0]["id"] == article_id
        assert body["articles"][0]["titleJa"] == "記事タイトル"
