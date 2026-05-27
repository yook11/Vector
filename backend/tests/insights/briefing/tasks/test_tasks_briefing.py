"""briefing tasks (dispatcher + per-category subtask) のテスト。

検証する観点:
- ``schedule`` ラベルに JST 月曜 00:05 相当の cron が登録される
- dispatcher が全カテゴリに対して subtask を kiq する + 週次 anchor 監査を焼く
- dispatcher 自体が落ちたとき anchor 失敗監査を焼いて re-raise する
- subtask は Ready None で early return、Ready あり で Service.execute を呼ぶ
- subtask は失敗を audit に焼いてから raise する (taskiq の retry を維持)
- subtask の ``retry_exhausted`` は ``is_last_attempt(ctx)`` (retry_count /
  max_retries label) で決まる
"""

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


def _ctx_with_session_factory(
    *, retry_count: int = 0, max_retries: int = 2
) -> MagicMock:
    """``ctx.message.labels`` を持つ taskiq Context モック。

    ``is_last_attempt(ctx)`` は labels の ``retry_count`` / ``max_retries`` を
    読むので、retry 軸を切り替える試験ではここを差し替える。
    """
    ctx = MagicMock()
    session = MagicMock()
    session.commit = AsyncMock()  # await session.commit() を許容
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=session_ctx)
    ctx.state.session_factory = factory
    ctx.message.labels = {"retry_count": retry_count, "max_retries": max_retries}
    return ctx


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
    async def test_kiq_per_category(self) -> None:
        """全カテゴリに対して subtask が 1 つずつ kiq される。"""
        from app.queue.tasks import briefing

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
            audit_cls.return_value.append_dispatched = AsyncMock()
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
            patch(
                "app.queue.tasks.briefing.DeepSeekBriefingGenerator",
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
            ),
            patch(
                "app.queue.tasks.briefing.DeepSeekBriefingGenerator",
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
            patch(
                "app.queue.tasks.briefing.DeepSeekBriefingGenerator",
                return_value=MagicMock(),
            ),
            # audit 書込は別 PR で検証 (Service / repo 経由)。task 層は raise 伝播
            # だけを確認するため、ここでは BriefingAuditRepository を no-op patch。
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
        retry_count: int,
        max_retries: int,
    ) -> tuple[MagicMock, MagicMock]:
        """指定 exc + retry 軸で subtask を 1 回流し、audit cls の mock を返す。

        Returns:
            (audit_cls_mock, append_failure_mock)
        """
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory(
            retry_count=retry_count, max_retries=max_retries
        )
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
            patch(
                "app.queue.tasks.briefing.DeepSeekBriefingGenerator",
                return_value=MagicMock(),
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
        """中間 retry (retry_count=1, max=2) は ``retry_exhausted=None`` で焼く。

        extrinsic な give-up timing は retry 上限到達時のみ True、中間は
        None (= JSON null) で payload に出さない。
        """
        exc = BriefingConfigurationError("DEEPSEEK_API_KEY missing")
        _, append_failure = await self._run_with_exc(exc, retry_count=1, max_retries=2)

        append_failure.assert_awaited_once()
        kwargs = append_failure.await_args.kwargs
        assert kwargs["exc"] is exc
        assert kwargs["retry_exhausted"] is None
        assert kwargs["ai_model"] == "deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_records_failure_with_retry_exhausted_on_last_attempt(self) -> None:
        """最終 retry (retry_count=2, max=2) は ``retry_exhausted=True`` で焼く。

        ``is_last_attempt(ctx)`` が True を返すケース。consumer は
        ``payload @> '{"retry_exhausted": true}'`` で give-up を集計する。
        """
        exc = BriefingConfigurationError("DEEPSEEK_API_KEY missing")
        _, append_failure = await self._run_with_exc(exc, retry_count=2, max_retries=2)

        kwargs = append_failure.await_args.kwargs
        assert kwargs["retry_exhausted"] is True


class TestDispatcherAudit:
    """dispatcher の週次 anchor 監査経路 (success + failure)。"""

    @pytest.mark.asyncio
    async def test_writes_dispatched_anchor_after_kiq(self) -> None:
        """全 subtask kiq 後に ``append_dispatched`` を 1 回呼ぶ。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()

        cat1 = MagicMock(id=1)
        cat2 = MagicMock(id=2)
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[cat1, cat2])
        rows = MagicMock()
        rows.scalars = MagicMock(return_value=scalars)
        ctx.state.session_factory.return_value.__aenter__.return_value.execute = (
            AsyncMock(return_value=rows)
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
            ),
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
        ):
            audit_cls.return_value.append_dispatched = AsyncMock()
            audit_cls.return_value.append_dispatcher_failure = AsyncMock()
            audit_cls.return_value.append_unexpected_dispatcher_failure = AsyncMock()
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        audit_cls.return_value.append_dispatched.assert_awaited_once_with(
            week_start=date(2026, 4, 20),
            category_count=2,
        )
        audit_cls.return_value.append_dispatcher_failure.assert_not_awaited()
        audit_cls.return_value.append_unexpected_dispatcher_failure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writes_dispatcher_failure_when_session_raises(self) -> None:
        """dispatcher 本体の想定外例外で failure anchor が焼かれ re-raise。

        ``broker_briefing`` は ``max_retries=0`` で初回即 give-up のため
        ``retry_exhausted=True`` 固定で焼く (semantic は repo 側で保証)。
        """
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        # 最初に session_factory() で session を取りに行ったところで例外を出す
        boom = RuntimeError("session factory boom")
        ctx.state.session_factory.return_value.__aenter__ = AsyncMock(side_effect=boom)

        with (
            patch(
                "app.queue.tasks.briefing.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch("app.queue.tasks.briefing.BriefingAuditRepository") as audit_cls,
        ):
            audit_cls.return_value.append_dispatched = AsyncMock()
            audit_cls.return_value.append_dispatcher_failure = AsyncMock()
            audit_cls.return_value.append_unexpected_dispatcher_failure = AsyncMock()
            # 失敗 audit を焼くときの session も同じ factory を通るため、ここでは
            # __aenter__ を最初の呼び出しだけ raise させて、後段の audit 用 session は
            # 通常 session を返すように差し替える。
            normal_session = MagicMock()
            normal_session.commit = AsyncMock()
            normal_session_ctx = MagicMock()
            normal_session_ctx.__aenter__ = AsyncMock(return_value=normal_session)
            normal_session_ctx.__aexit__ = AsyncMock(return_value=None)
            ctx.state.session_factory.side_effect = [
                # 1 回目: dispatch 本体の session — __aenter__ で raise
                MagicMock(
                    __aenter__=AsyncMock(side_effect=boom),
                    __aexit__=AsyncMock(return_value=None),
                ),
                # 2 回目: 失敗 audit 用 session — 通常通り context manager
                normal_session_ctx,
            ]
            with pytest.raises(RuntimeError, match="session factory boom"):
                await briefing.dispatch_weekly_briefings(ctx=ctx)

        audit_cls.return_value.append_dispatcher_failure.assert_not_awaited()
        append_unexpected = audit_cls.return_value.append_unexpected_dispatcher_failure
        append_unexpected.assert_awaited_once()
        kwargs = append_unexpected.await_args.kwargs
        assert kwargs["week_start"] == date(2026, 4, 20)
        assert kwargs["exc"] is boom
