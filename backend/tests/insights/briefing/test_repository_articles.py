"""BriefingArticleRepository.fetch のテスト (week × category フィルタ)。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.repository import BriefingArticleRepository
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
        # 順序は InScopeAssessment.id 昇順 (公開 /news id 空間)
        assert result[0].title_ja == "記事A"
        assert result[1].title_ja == "記事B"

    @pytest.mark.asyncio
    async def test_fetch_id_is_assessment_id_not_article_id(
        self,
        db_session: AsyncSession,
        categories: dict[str, Category],
        sample_source,
        seed_briefing_analysis,
    ) -> None:
        """ArticleInput.id は公開 /news id 空間 (InScopeAssessment.id)。

        decoy record を先に 1 件 INSERT して source article id と
        InScopeAssessment.id をずらし、ArticleInput.id == analysis.id を保証する。
        """
        from app.models.analyzable_article_record import AnalyzableArticleRecord

        # decoy article (assessment なし) を先に INSERT してシーケンスをずらす
        db_session.add(
            AnalyzableArticleRecord(
                source_id=sample_source.id,
                source_url="https://example.com/decoy",
                original_title="decoy",
                original_content="x" * 60,
            )
        )
        await db_session.flush()

        ai = categories["ai"]
        analysis = await seed_briefing_analysis(
            category_id=ai.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="記事Z",
        )
        await db_session.commit()

        # decoy が効いていることの前提 assert: 両 id がずれていなければ判別力が無い
        from sqlalchemy import select

        from app.models.article_curation import ArticleCuration

        article_id = (
            await db_session.execute(
                select(ArticleCuration.analyzable_article_id).where(
                    ArticleCuration.id == analysis.curation_id
                )
            )
        ).scalar_one()
        assert analysis.id != article_id, (
            "decoy が効いていない: source article id と assessment id が一致して"
            "いるためテストが id 空間を判別できない"
        )

        repo = BriefingArticleRepository(db_session)
        result = await repo.fetch(week_start=date(2026, 4, 20), category_id=ai.id)
        assert len(result) == 1
        # 公開 /news id 空間 (InScopeAssessment.id) であって source article id ではない
        assert result[0].id == analysis.id
        assert result[0].id != article_id

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
