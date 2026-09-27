"""BriefingRepository.load_ready_build_facts のテスト。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.domain.ready import (
    BriefingArticle,
    BriefingReadyBuildFacts,
)
from app.insights.briefing.repository import BriefingRepository
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.weekly_briefing import WeeklyBriefing

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


class TestCategoryAndExistence:
    @pytest.mark.asyncio
    async def test_returns_none_when_category_missing(
        self, db_session: AsyncSession
    ) -> None:
        repo = BriefingRepository(db_session)

        facts = await repo.load_ready_build_facts(
            week_start=date(2026, 4, 20), category_id=999_999
        )

        assert facts is None

    @pytest.mark.asyncio
    async def test_returns_category_and_empty_week(
        self, db_session: AsyncSession, categories: dict[str, Category]
    ) -> None:
        repo = BriefingRepository(db_session)

        facts = await repo.load_ready_build_facts(
            week_start=date(2026, 4, 20), category_id=categories["ai"].id
        )

        assert facts == BriefingReadyBuildFacts(
            category_slug="ai",
            category_name="AI",
            already_generated=False,
            articles=(),
        )

    @pytest.mark.asyncio
    async def test_marks_already_generated_only_for_same_week(
        self, db_session: AsyncSession, categories: dict[str, Category]
    ) -> None:
        db_session.add(
            WeeklyBriefing(
                week_start_date=date(2026, 4, 20),
                category_id=categories["ai"].id,
                headline="h",
                summary="s",
                chapters=[],
                key_articles=[],
                watch_points=[],
                model_name="deepseek-v4-pro",
                input_article_count=1,
            )
        )
        await db_session.commit()
        repo = BriefingRepository(db_session)

        same_week = await repo.load_ready_build_facts(
            week_start=date(2026, 4, 20), category_id=categories["ai"].id
        )
        next_week = await repo.load_ready_build_facts(
            week_start=date(2026, 4, 27), category_id=categories["ai"].id
        )

        assert same_week is not None and next_week is not None
        assert (same_week.already_generated, next_week.already_generated) == (
            True,
            False,
        )


class TestWeekArticles:
    @pytest.mark.asyncio
    async def test_returns_articles_in_week_and_category_by_id(
        self,
        db_session: AsyncSession,
        categories: dict[str, Category],
        seed_briefing_analysis,
    ) -> None:
        ai = categories["ai"]
        first = await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="記事A",
            summary="要約A",
        )
        second = await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 25, 12, 0, tzinfo=JST),
            translated_title="記事B",
            summary="要約B",
        )
        # 同じ週でも別カテゴリの記事は含めない。
        await seed_briefing_analysis(
            category_id=categories["bio"].id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await db_session.commit()

        facts = await BriefingRepository(db_session).load_ready_build_facts(
            week_start=date(2026, 4, 20), category_id=ai.id
        )

        assert facts is not None
        assert facts.articles == (
            BriefingArticle(
                analyzed_article_id=first.id, translated_title="記事A", summary="要約A"
            ),
            BriefingArticle(
                analyzed_article_id=second.id, translated_title="記事B", summary="要約B"
            ),
        )

    @pytest.mark.asyncio
    async def test_article_id_is_analyzed_article_id_not_source_article_id(
        self,
        db_session: AsyncSession,
        categories: dict[str, Category],
        sample_source,
        seed_briefing_analysis,
    ) -> None:
        """素材の id は公開記事 id (AnalyzedArticleRecord.id) で、元記事の id ではない。

        decoy record を先に 1 件 INSERT して両 id をずらし、取り違えを判別する。
        """
        db_session.add(
            AnalyzableArticleRecord(
                source_id=sample_source.id,
                source_url="https://example.com/decoy",
                original_title="decoy",
                original_content="x" * 60,
                published_at=datetime(2026, 4, 20, 9, 0, tzinfo=JST),
            )
        )
        await db_session.flush()

        ai = categories["ai"]
        analysis = await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await db_session.commit()
        source_article_id = (
            await db_session.execute(
                select(ArticleCuration.analyzable_article_id).where(
                    ArticleCuration.id == analysis.curation_id
                )
            )
        ).scalar_one()
        # decoy が効いていることの前提 assert (一致していたら下の判別が空虚になる)
        assert analysis.id != source_article_id

        facts = await BriefingRepository(db_session).load_ready_build_facts(
            week_start=date(2026, 4, 20), category_id=ai.id
        )

        assert facts is not None
        assert [a.analyzed_article_id for a in facts.articles] == [analysis.id]

    @pytest.mark.asyncio
    async def test_uses_jst_monday_to_monday_window(
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
        in_week = await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 20, 0, 0, tzinfo=JST),
        )
        # 翌週 (2026-04-27 週) 初日 00:00 JST → 含まれない
        await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 27, 0, 0, tzinfo=JST),
        )
        await db_session.commit()

        facts = await BriefingRepository(db_session).load_ready_build_facts(
            week_start=date(2026, 4, 20), category_id=ai.id
        )

        assert facts is not None
        assert [a.analyzed_article_id for a in facts.articles] == [in_week.id]
