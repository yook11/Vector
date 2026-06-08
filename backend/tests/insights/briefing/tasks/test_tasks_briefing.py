"""briefing tasks (dispatcher + per-category subtask) のテスト。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.insights.briefing.application.service import GeneratedBriefing
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.llm.errors import BriefingConfigurationError
from app.queue.messages.briefing import BriefingTaskInput

JST = ZoneInfo("Asia/Tokyo")


def _ctx_with_session_factory(*, retries: int = 0, max_retries: int = 2) -> MagicMock:
    """``ctx.message.labels`` を持つ taskiq Context モック。"""
    ctx = MagicMock()
    session = MagicMock()
    session.commit = AsyncMock()
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=session_ctx)
    ctx.state.session_factory = factory
    # generator は composition root が broker_briefing 起動時に state へ wire する。
    # task はそれを読んで service に DI するため、ctx.state に明示的に置く。
    ctx.state.briefing_generator = MagicMock()
    # taskiq SimpleRetryMiddleware が書く label は "_retries" (0..max_retries-1)
    ctx.message.labels = {"_retries": retries, "max_retries": max_retries}
    return ctx


def _category_rows(*category_ids: int) -> MagicMock:
    """session.execute の戻り値 mock を作る。"""
    scalars = MagicMock()
    scalars.all = MagicMock(
        return_value=[MagicMock(id=category_id) for category_id in category_ids]
    )
    rows = MagicMock()
    rows.scalars = MagicMock(return_value=scalars)
    return rows


def _patch_dispatch_audit_methods(audit_cls: MagicMock) -> None:
    audit_cls.return_value.append_category_enqueued = AsyncMock()
    audit_cls.return_value.append_category_enqueue_failed = AsyncMock()
    audit_cls.return_value.append_dispatch_completed = AsyncMock()
    audit_cls.return_value.append_dispatch_category_master_load_failed = AsyncMock()


class TestSchedule:
    def test_dispatch_cron_matches_jst_monday_midnight(self) -> None:
        from app.queue.tasks import briefing

        schedule = briefing.dispatch_weekly_briefings.labels.get("schedule")
        assert isinstance(schedule, list)
        assert any(entry.get("cron") == "5 15 * * 0" for entry in schedule)

    def test_subtask_has_retry_2(self) -> None:
        from app.queue.tasks import briefing

        labels = briefing.generate_briefing_for_category.labels
        assert labels.get("max_retries") == 2
        assert labels.get("retry_on_error") is True


class TestDispatcher:
    @pytest.mark.asyncio
    async def test_kiq_per_category_and_audits_success_rows(self) -> None:
        """全カテゴリに対して subtask を kiq し、カテゴリ単位成功を焼く。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        ctx.state.session_factory.return_value.__aenter__.return_value.execute = (
            AsyncMock(return_value=_category_rows(1, 2, 3))
        )

        with (
            patch(
                "app.queue.tasks.briefing.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch.object(
                briefing.generate_briefing_for_category,
                "kiq",
                new=AsyncMock(),
            ) as kiq,
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
        ):
            _patch_dispatch_audit_methods(audit_cls)
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        assert kiq.await_count == 3
        called_inputs = [call.args[0] for call in kiq.await_args_list]
        for inp in called_inputs:
            assert isinstance(inp, BriefingTaskInput)
            assert inp.week_start == date(2026, 4, 20)
        assert {inp.category_id for inp in called_inputs} == {1, 2, 3}

        append_enqueued = audit_cls.return_value.append_category_enqueued
        assert append_enqueued.await_count == 3
        assert [
            call.kwargs["category_id"] for call in append_enqueued.await_args_list
        ] == [1, 2, 3]
        audit_cls.return_value.append_dispatch_completed.assert_awaited_once_with(
            week_start=date(2026, 4, 20),
            selected_category_count=3,
            enqueued_category_count=3,
            failed_category_count=0,
        )

    @pytest.mark.asyncio
    async def test_continues_after_one_category_enqueue_failure(self) -> None:
        """1カテゴリの .kiq() 失敗は焼いて続行し、dispatcher は raise しない。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        ctx.state.session_factory.return_value.__aenter__.return_value.execute = (
            AsyncMock(return_value=_category_rows(1, 2, 3))
        )
        boom = RuntimeError("broker down")

        with (
            patch(
                "app.queue.tasks.briefing.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch.object(
                briefing.generate_briefing_for_category,
                "kiq",
                new=AsyncMock(side_effect=[None, boom, None]),
            ) as kiq,
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
        ):
            _patch_dispatch_audit_methods(audit_cls)
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        assert kiq.await_count == 3
        audit_cls.return_value.append_category_enqueue_failed.assert_awaited_once_with(
            week_start=date(2026, 4, 20),
            category_id=2,
            exc=boom,
        )
        audit_cls.return_value.append_dispatch_completed.assert_awaited_once_with(
            week_start=date(2026, 4, 20),
            selected_category_count=3,
            enqueued_category_count=2,
            failed_category_count=1,
        )

    @pytest.mark.asyncio
    async def test_zero_categories_writes_completed_summary_with_zero_counts(
        self,
    ) -> None:
        """カテゴリマスタ 0 件は専用 outcome ではなく 0 counts summary で表す。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        ctx.state.session_factory.return_value.__aenter__.return_value.execute = (
            AsyncMock(return_value=_category_rows())
        )

        with (
            patch(
                "app.queue.tasks.briefing.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch.object(
                briefing.generate_briefing_for_category,
                "kiq",
                new=AsyncMock(),
            ) as kiq,
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
        ):
            _patch_dispatch_audit_methods(audit_cls)
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        kiq.assert_not_awaited()
        audit_cls.return_value.append_dispatch_completed.assert_awaited_once_with(
            week_start=date(2026, 4, 20),
            selected_category_count=0,
            enqueued_category_count=0,
            failed_category_count=0,
        )

    @pytest.mark.asyncio
    async def test_writes_dispatch_load_failure_when_category_master_read_raises(
        self,
    ) -> None:
        """カテゴリマスタ取得失敗は固定 outcome を焼いて re-raise する。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        boom = RuntimeError("session factory boom")
        normal_session = MagicMock()
        normal_session.commit = AsyncMock()
        normal_session_ctx = MagicMock()
        normal_session_ctx.__aenter__ = AsyncMock(return_value=normal_session)
        normal_session_ctx.__aexit__ = AsyncMock(return_value=None)
        ctx.state.session_factory.side_effect = [
            MagicMock(
                __aenter__=AsyncMock(side_effect=boom),
                __aexit__=AsyncMock(return_value=None),
            ),
            normal_session_ctx,
        ]

        with (
            patch(
                "app.queue.tasks.briefing.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
            pytest.raises(RuntimeError, match="session factory boom"),
        ):
            _patch_dispatch_audit_methods(audit_cls)
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        append_failed = (
            audit_cls.return_value.append_dispatch_category_master_load_failed
        )
        append_failed.assert_awaited_once()
        kwargs = append_failed.await_args.kwargs
        assert kwargs["week_start"] == date(2026, 4, 20)
        assert kwargs["exc"] is boom


class TestSubtask:
    @pytest.mark.asyncio
    async def test_skips_when_ready_is_none_and_audits_existing(self) -> None:
        from app.queue.tasks import briefing

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
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
        ):
            audit_cls.return_value.append_generation_already_exists = AsyncMock()
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        service.execute.assert_not_awaited()
        audit_cls.return_value.append_generation_already_exists.assert_awaited_once_with(
            week_start=date(2026, 4, 20),
            category_id=1,
        )

    @pytest.mark.asyncio
    async def test_invokes_service_when_ready(self) -> None:
        from app.queue.tasks import briefing

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
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ) as service_cls,
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        service.execute.assert_awaited_once_with(ready)
        # composition root が broker_briefing 起動時に state へ wire した generator が
        # そのまま service に DI される (Pure DI 経路の不変条件)。
        assert service_cls.call_args.args[1] is ctx.state.briefing_generator

    @pytest.mark.asyncio
    async def test_propagates_service_exception(self) -> None:
        from app.queue.tasks import briefing

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
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
            pytest.raises(RuntimeError, match="LLM down"),
        ):
            audit_cls.return_value.append_unexpected_failure = AsyncMock()
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )


class TestSubtaskFailureAudit:
    """subtask の失敗監査経路。"""

    @staticmethod
    async def _run_with_exc(
        exc: BaseException,
        *,
        retries: int,
        max_retries: int,
    ) -> tuple[MagicMock, MagicMock]:
        """指定 exc + retry 軸で subtask を 1 回流し、audit cls の mock を返す。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory(retries=retries, max_retries=max_retries)
        ready = ReadyForBriefing(
            week_start=date(2026, 4, 20), category_id=1, force=False
        )
        service = MagicMock()
        service.execute = AsyncMock(side_effect=exc)
        service._llm.MODEL = "deepseek-v4-pro"

        with (
            patch.object(
                ReadyForBriefing,
                "try_advance_from",
                new=AsyncMock(return_value=ready),
            ),
            patch(
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
        ):
            audit_cls.return_value.append_failure = AsyncMock()
            with pytest.raises(type(exc)):
                await briefing.generate_briefing_for_category(
                    BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                    ctx=ctx,
                )
        return audit_cls, audit_cls.return_value.append_failure

    @pytest.mark.asyncio
    async def test_records_failure_then_reraises_middle_attempt(self) -> None:
        exc = BriefingConfigurationError("DEEPSEEK_API_KEY missing")
        # 非最終試行: _retries=0 < max_retries-1=1 → retry_exhausted=None
        _, append_failure = await self._run_with_exc(exc, retries=0, max_retries=2)

        append_failure.assert_awaited_once()
        kwargs = append_failure.await_args.kwargs
        assert kwargs["exc"] is exc
        assert kwargs["retry_exhausted"] is None
        assert kwargs["ai_model"] == "deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_records_failure_with_retry_exhausted_on_last_attempt(self) -> None:
        exc = BriefingConfigurationError("DEEPSEEK_API_KEY missing")
        # 最終試行: _retries=max_retries-1=1
        _, append_failure = await self._run_with_exc(exc, retries=1, max_retries=2)

        kwargs = append_failure.await_args.kwargs
        assert kwargs["retry_exhausted"] is True
