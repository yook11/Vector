"""WeeklyBriefingService.execute の主要パステスト (LLM mock)。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx  # noqa: TID251 (テスト内 mock 構築のため、実通信なし)
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.stages.briefing import (
    OUTCOME_BRIEFING_GENERATION_COMPLETED,
    OUTCOME_BRIEFING_GENERATION_INPUT_EMPTY,
)
from app.insights.briefing.application.notifier import (
    FrontendRevalidateNotifier,
    NullBriefingNotifier,
)
from app.insights.briefing.application.service import WeeklyBriefingService
from app.insights.briefing.domain.briefing import (
    BriefingStory,
    WeeklyBriefingContent,
)
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.repository.briefings import BriefingRepository
from app.models.category import Category
from app.models.pipeline_event import PipelineEvent

JST = ZoneInfo("Asia/Tokyo")


def _factory_for(db_session) -> async_sessionmaker:
    """テスト中の db_session をそのまま返す session_factory (Service 注入用)。"""
    return async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
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
    async def test_service_persists_even_when_frontend_revalidate_http_error(
        self,
        db_session,
        ai_category: Category,
        seed_briefing_analysis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """revalidate HTTP 失敗は briefing 生成成功を失敗扱いにしない。

        no-raise 契約は ``FrontendRevalidateNotifier`` が担う。Service 経由でも
        保存成功後に通知を試み、HTTP error が warn 降格されることを固定する。
        """
        await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await db_session.commit()

        captured: list[tuple[str, str, str, bytes]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(
                (
                    request.method,
                    str(request.url),
                    request.headers["authorization"],
                    request.read(),
                )
            )
            return httpx.Response(500, json={"error": "boom"})

        transport = httpx.MockTransport(handler)
        original_init = httpx.AsyncClient.__init__

        def patched_init(
            self: httpx.AsyncClient, *args: object, **kwargs: object
        ) -> None:
            kwargs["transport"] = transport
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

        notifier = FrontendRevalidateNotifier(
            frontend_base_url="http://frontend:3000",
            secret="test-secret-32characters-long-xxxx",
        )
        llm = _llm_mock(headline="OK", overview="OVERVIEW")
        service = WeeklyBriefingService(_factory_for(db_session), llm, notifier)
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        outcome = await service.execute(ready)

        assert outcome.persisted is True
        assert captured == [
            (
                "POST",
                "http://frontend:3000/api/internal/revalidate",
                "Bearer test-secret-32characters-long-xxxx",
                b'{"tags":["briefing:ai","briefing:list"]}',
            )
        ]

        repo = BriefingRepository(db_session)
        saved = await repo.find_by(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )
        assert saved is not None
        assert saved.headline == "OK"
        assert saved.overview == "OVERVIEW"


class TestAuditIntegration:
    """SUCCEEDED 同 tx / REJECTED 別 tx の audit 書込検証。"""

    @pytest.mark.asyncio
    async def test_writes_succeeded_audit_alongside_briefing(
        self,
        db_session,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """成功時に briefing 行と SUCCEEDED audit 行が同時に観測される。

        Service が write tx 内で audit を append し commit するため、SUCCEEDED 行は
        briefing UPSERT と atomic (D5)。「briefing 行はあるが SUCCEEDED 無し」の
        偽ギャップが構造的に発生しない。
        """
        await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        await db_session.commit()

        service = WeeklyBriefingService(
            _factory_for(db_session), _llm_mock(), NullBriefingNotifier()
        )
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        await service.execute(ready)

        rows = (
            (
                await db_session.execute(
                    select(PipelineEvent).where(
                        PipelineEvent.outcome_code
                        == OUTCOME_BRIEFING_GENERATION_COMPLETED
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        ev = rows[0]
        assert ev.stage == "briefing"
        assert ev.event_type == "succeeded"
        assert ev.retryability is None
        assert ev.payload["category_id"] == ai_category.id
        assert ev.payload["category_slug"] == "ai"
        assert ev.payload["article_count"] == 1
        assert ev.payload["ai_model"] == "deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_writes_rejected_audit_when_no_articles(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        """articles 0 件 (steady-state 異常系) で REJECTED audit が焼かれる。

        LLM 呼出も write tx も走らず、read tx 直後の別 tx で 1 行記録する。
        retryability は NULL (retry 概念外、event_type で完結)。
        """
        service = WeeklyBriefingService(
            _factory_for(db_session), _llm_mock(), NullBriefingNotifier()
        )
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )

        await service.execute(ready)

        rows = (
            (
                await db_session.execute(
                    select(PipelineEvent).where(
                        PipelineEvent.outcome_code
                        == OUTCOME_BRIEFING_GENERATION_INPUT_EMPTY
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        ev = rows[0]
        assert ev.event_type == "rejected"
        assert ev.retryability is None
        assert ev.payload["article_count"] == 0
