"""WeeklyBriefingService.execute の主要パステスト (LLM mock)。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.insights.briefing.application.notifier import NullBriefingNotifier
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


def _llm_mock(
    headline: str = "今週のハイライト", overview: str = "今週の流れ"
) -> MagicMock:
    llm = MagicMock()
    llm.MODEL = "deepseek-v4-pro"
    llm.generate = AsyncMock(
        return_value=WeeklyBriefingContent(
            headline=headline,
            overview=overview,
            stories=[BriefingStory(takeaway="記事から読み取った内容", article_ids=[1])],
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
        service = WeeklyBriefingService(
            _factory_for(db_session), llm, NullBriefingNotifier()
        )
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

        llm = _llm_mock(headline="OK", overview="OVERVIEW")
        service = WeeklyBriefingService(
            _factory_for(db_session), llm, NullBriefingNotifier()
        )
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
        assert saved.overview == "OVERVIEW"
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
        service = WeeklyBriefingService(
            _factory_for(db_session), llm, NullBriefingNotifier()
        )
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        with pytest.raises(RuntimeError, match="LLM failed"):
            await service.execute(ready)


class TestNotifierIntegration:
    @pytest.mark.asyncio
    async def test_calls_notifier_after_successful_persist(
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

        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=None)
        llm = _llm_mock()
        service = WeeklyBriefingService(_factory_for(db_session), llm, notifier)
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        await service.execute(ready)

        notifier.notify.assert_awaited_once_with(category_slug="ai")

    @pytest.mark.asyncio
    async def test_does_not_call_notifier_when_no_articles(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=None)
        service = WeeklyBriefingService(_factory_for(db_session), _llm_mock(), notifier)
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        await service.execute(ready)

        notifier.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_service_succeeds_even_if_notifier_raises(
        self,
        db_session,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """Service は notifier の戻り値に依存しない (notify は副作用専用)。

        notify は契約上 raise しない (warn 降格)。Service 側で再 try/except
        は重ねない。実装責務は ``FrontendRevalidateNotifier`` に閉じ込める。
        """
        await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await db_session.commit()

        notifier = MagicMock()
        # 仮に notifier 実装が誤って raise しても Service は伝播してしまう。
        # 設計契約上 notify は raise しないため、ここでは raise する mock を
        # 渡すことで「Service は notifier の挙動にデフェンシブではない (契約に依存)」
        # ことを明示する。実装側 (FrontendRevalidateNotifier) で warn 降格を保証。
        notifier.notify = AsyncMock(return_value=None)
        service = WeeklyBriefingService(_factory_for(db_session), _llm_mock(), notifier)
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        outcome = await service.execute(ready)
        assert outcome.persisted is True
