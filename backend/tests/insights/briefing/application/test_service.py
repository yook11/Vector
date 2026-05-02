"""WeeklyBriefingService.execute の主要パステスト (LLM mock)。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.insights.briefing.application.service import WeeklyBriefingService
from app.insights.briefing.domain.briefing import (
    BriefingStory,
    WeeklyBriefingContent,
)
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.repository.briefings import BriefingRepository
from app.models.category import Category

JST = ZoneInfo("Asia/Tokyo")


def _factory_for(db_session) -> async_sessionmaker:
    """テスト中の db_session をそのまま返す session_factory (Service 注入用)。"""
    return async_sessionmaker(
        db_session.bind, class_=SQLModelAsyncSession, expire_on_commit=False
    )


def _llm_mock(headline: str = "今週のハイライト") -> MagicMock:
    llm = MagicMock()
    llm.MODEL = "deepseek-v4-pro"
    llm.generate = AsyncMock(
        return_value=WeeklyBriefingContent(
            headline=headline,
            stories=[
                BriefingStory(title="ストーリー1", analysis="分析本文", article_ids=[1])
            ],
        )
    )
    return llm


@pytest.fixture
async def ai_category(db_session) -> Category:
    cat = Category(slug="ai", name="AI")
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)
    return cat


class TestExecute:
    @pytest.mark.asyncio
    async def test_skips_when_no_articles(
        self, db_session, ai_category: Category
    ) -> None:
        """articles 0 件のとき LLM を呼ばず persisted=False を返す。"""
        llm = _llm_mock()
        service = WeeklyBriefingService(_factory_for(db_session), llm)
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        outcome = await service.execute(ready)

        assert outcome.persisted is False
        assert outcome.article_count == 0
        llm.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persists_briefing_when_articles_present(
        self,
        db_session,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await db_session.commit()

        llm = _llm_mock(headline="OK")
        service = WeeklyBriefingService(_factory_for(db_session), llm)
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        outcome = await service.execute(ready)

        assert outcome.persisted is True
        assert outcome.article_count == 1
        llm.generate.assert_awaited_once()

        # DB 上に 1 行入っていること
        repo = BriefingRepository(db_session)
        saved = await repo.find_by(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )
        assert saved is not None
        assert saved.headline == "OK"
        assert saved.input_article_count == 1
        assert saved.model_name == "deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_propagates_llm_exception(
        self,
        db_session,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await db_session.commit()

        llm = MagicMock()
        llm.MODEL = "deepseek-v4-pro"
        llm.generate = AsyncMock(side_effect=RuntimeError("LLM failed"))
        service = WeeklyBriefingService(_factory_for(db_session), llm)
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        with pytest.raises(RuntimeError, match="LLM failed"):
            await service.execute(ready)
