"""BriefingArticleRepository.fetch のテスト (week × category フィルタ)。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.repository.articles import BriefingArticleRepository
from app.models.category import Category

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
async def categories(db_session: AsyncSession) -> dict[str, Category]:
    cats = [Category(slug="ai", name="AI"), Category(slug="bio", name="Bio")]
    for c in cats:
        db_session.add(c)
    await db_session.commit()
    for c in cats:
        await db_session.refresh(c)
    return {str(c.slug): c for c in cats}


class TestFetch:
    @pytest.mark.asyncio
    async def test_returns_articles_in_week_and_category(
        self,
        db_session: AsyncSession,
        categories: dict[str, Category],
        seed_briefing_analysis,
    ) -> None:
        ai = categories["ai"]
        await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="記事A",
            summary="要約A",
        )
        await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 25, 12, 0, tzinfo=JST),
            translated_title="記事B",
            summary="要約B",
        )
        await db_session.commit()

        repo = BriefingArticleRepository(db_session)
        result = await repo.fetch(week_start=date(2026, 4, 20), category_id=ai.id)
        assert len(result) == 2
        # 順序は article_id 昇順
        assert result[0].title_ja == "記事A"
        assert result[1].title_ja == "記事B"

    @pytest.mark.asyncio
    async def test_excludes_other_categories(
        self,
        db_session: AsyncSession,
        categories: dict[str, Category],
        seed_briefing_analysis,
    ) -> None:
        await seed_briefing_analysis(
            category_id=categories["ai"].id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await seed_briefing_analysis(
            category_id=categories["bio"].id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await db_session.commit()

        repo = BriefingArticleRepository(db_session)
        result = await repo.fetch(
            week_start=date(2026, 4, 20), category_id=categories["ai"].id
        )
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_excludes_outside_week_window(
        self,
        db_session: AsyncSession,
        categories: dict[str, Category],
        seed_briefing_analysis,
    ) -> None:
        ai = categories["ai"]
        # 前週 (2026-04-13 週) 末日 23:59 JST → 含まれない
        await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 19, 23, 59, tzinfo=JST),
        )
        # 当週 (2026-04-20 週) 初日 00:00 JST → 含まれる
        await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 20, 0, 0, tzinfo=JST),
        )
        # 翌週 (2026-04-27 週) 初日 00:00 JST → 含まれない
        await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 27, 0, 0, tzinfo=JST),
        )
        await db_session.commit()

        repo = BriefingArticleRepository(db_session)
        result = await repo.fetch(week_start=date(2026, 4, 20), category_id=ai.id)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_match(
        self,
        db_session: AsyncSession,
        categories: dict[str, Category],
    ) -> None:
        repo = BriefingArticleRepository(db_session)
        result = await repo.fetch(
            week_start=date(2026, 4, 20), category_id=categories["ai"].id
        )
        assert result == []
