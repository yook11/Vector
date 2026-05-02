"""briefing tasks (dispatcher + per-category subtask) のテスト。

検証する観点:
- ``schedule`` ラベルに JST 月曜 00:05 相当の cron が登録される
- dispatcher が全カテゴリに対して subtask を kiq する
- subtask は Ready None で early return、Ready あり で Service.execute を呼ぶ
- subtask の例外は捕まえずに伝播する (failure_visibility)
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.insights.briefing.application.service import GeneratedBriefing
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.domain.task_input import BriefingTaskInput

JST = ZoneInfo("Asia/Tokyo")


def _ctx_with_session_factory() -> MagicMock:
    ctx = MagicMock()
    session = MagicMock()
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=session_ctx)
    ctx.state.session_factory = factory
    return ctx


class TestSchedule:
    def test_dispatch_cron_matches_jst_monday_midnight(self) -> None:
        from app.insights.briefing.tasks import briefing

        schedule = briefing.dispatch_weekly_briefings.labels.get("schedule")
        assert isinstance(schedule, list)
        assert any(entry.get("cron") == "5 15 * * 0" for entry in schedule)

    def test_subtask_has_retry_2(self) -> None:
        from app.insights.briefing.tasks import briefing

        labels = briefing.generate_briefing_for_category.labels
        assert labels.get("max_retries") == 2
        assert labels.get("retry_on_error") is True


class TestDispatcher:
    @pytest.mark.asyncio
    async def test_kiq_per_category(self) -> None:
        """全カテゴリに対して subtask が 1 つずつ kiq される。"""
        from app.insights.briefing.tasks import briefing

        ctx = _ctx_with_session_factory()

        cat1 = MagicMock(id=1)
        cat2 = MagicMock(id=2)
        cat3 = MagicMock(id=3)

        # session.execute → mock scalars().all() で categories を返す
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[cat1, cat2, cat3])
        rows = MagicMock()
        rows.scalars = MagicMock(return_value=scalars)
        ctx.state.session_factory.return_value.__aenter__.return_value.execute = (
            AsyncMock(return_value=rows)
        )

        with (
            patch(
                "app.insights.briefing.tasks.briefing.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch.object(
                briefing.generate_briefing_for_category,
                "kiq",
                new=AsyncMock(),
            ) as kiq,
        ):
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        assert kiq.await_count == 3
        called_inputs = [call.args[0] for call in kiq.await_args_list]
        for inp in called_inputs:
            assert isinstance(inp, BriefingTaskInput)
            assert inp.week_start == date(2026, 4, 20)
        assert {inp.category_id for inp in called_inputs} == {1, 2, 3}


class TestSubtask:
    @pytest.mark.asyncio
    async def test_skips_when_ready_is_none(self) -> None:
        from app.insights.briefing.tasks import briefing

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock()

        with (
            patch.object(
                ReadyForBriefing,
                "try_advance_from",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.insights.briefing.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            patch(
                "app.insights.briefing.tasks.briefing.DeepSeekBriefingGenerator",
                return_value=MagicMock(),
            ),
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        service.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invokes_service_when_ready(self) -> None:
        from app.insights.briefing.tasks import briefing

        ctx = _ctx_with_session_factory()
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=1, force=False
        )
        service = MagicMock()
        service.execute = AsyncMock(
            return_value=GeneratedBriefing(
                persisted=True,
                week_start=date(2026, 4, 20),
                category_id=1,
                article_count=10,
            )
        )

        with (
            patch.object(
                ReadyForBriefing,
                "try_advance_from",
                new=AsyncMock(return_value=ready),
            ),
            patch(
                "app.insights.briefing.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            patch(
                "app.insights.briefing.tasks.briefing.DeepSeekBriefingGenerator",
                return_value=MagicMock(),
            ),
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        service.execute.assert_awaited_once_with(ready)

    @pytest.mark.asyncio
    async def test_propagates_service_exception(self) -> None:
        from app.insights.briefing.tasks import briefing

        ctx = _ctx_with_session_factory()
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=1, force=False
        )
        service = MagicMock()
        service.execute = AsyncMock(side_effect=RuntimeError("LLM down"))

        with (
            patch.object(
                ReadyForBriefing,
                "try_advance_from",
                new=AsyncMock(return_value=ready),
            ),
            patch(
                "app.insights.briefing.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            patch(
                "app.insights.briefing.tasks.briefing.DeepSeekBriefingGenerator",
                return_value=MagicMock(),
            ),
            pytest.raises(RuntimeError, match="LLM down"),
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )
