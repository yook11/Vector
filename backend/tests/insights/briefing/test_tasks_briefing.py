"""briefing tasks のテスト。pipeline_stage span 配線含む。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.insights.briefing.domain.ready import (
    BriefingArticle,
    BriefingReadyBuildFacts,
    ReadyForBriefing,
)
from app.insights.briefing.repository import BriefingRepository
from app.insights.briefing.service import GeneratedBriefing
from app.queue.messages.briefing import BriefingTaskInput
from tests.logfire._span_helpers import pipeline_stage_attrs

JST = ZoneInfo("Asia/Tokyo")


def _ctx_with_session_factory() -> MagicMock:
    """session_factory と briefing generator を持つ taskiq Context モック。"""
    ctx = MagicMock()
    session = MagicMock()
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=session_ctx)
    ctx.state.session_factory = factory
    # generator は composition root が broker_briefing 起動時に state へ wire する。
    # task はそれを読んで service に DI するため、ctx.state に明示的に置く。
    ctx.state.briefing_generator = MagicMock()
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


_ARTICLE = BriefingArticle(analyzed_article_id=10, translated_title="t", summary="s")


def _facts(
    *,
    already_generated: bool = False,
    articles: tuple[BriefingArticle, ...] = (_ARTICLE,),
) -> BriefingReadyBuildFacts:
    return BriefingReadyBuildFacts(
        category_slug="ai",
        category_name="AI",
        already_generated=already_generated,
        articles=articles,
    )


def _patch_facts(facts: BriefingReadyBuildFacts | None):
    return patch.object(
        BriefingRepository,
        "load_ready_build_facts",
        new=AsyncMock(return_value=facts),
    )


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
        """全カテゴリに対して subtask を kiq する。"""
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
        ):
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        assert kiq.await_count == 3
        called_inputs = [call.args[0] for call in kiq.await_args_list]
        for inp in called_inputs:
            assert isinstance(inp, BriefingTaskInput)
            assert inp.week_start == date(2026, 4, 20)
        assert {inp.category_id for inp in called_inputs} == {1, 2, 3}

    @pytest.mark.asyncio
    async def test_continues_after_one_category_enqueue_failure(self) -> None:
        """1カテゴリの .kiq() 失敗でも残りを kiq し、dispatcher は raise しない。"""
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
        ):
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        assert kiq.await_count == 3

    @pytest.mark.asyncio
    async def test_zero_categories_logs_completed_summary_with_zero_counts(
        self,
    ) -> None:
        """カテゴリマスタ 0 件は専用の結果ではなく、件数 0 の完了 log で表す。"""
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
            capture_logs() as logs,
        ):
            await briefing.dispatch_weekly_briefings(ctx=ctx)

        kiq.assert_not_awaited()
        completed = [e for e in logs if e["event"] == "briefing_dispatch_completed"]
        assert [
            (
                e["week_start"],
                e["selected_category_count"],
                e["enqueued_category_count"],
                e["failed_category_count"],
            )
            for e in completed
        ] == [("2026-04-20", 0, 0, 0)]

    @pytest.mark.asyncio
    async def test_reraises_when_category_master_read_raises(self) -> None:
        """カテゴリマスタ取得失敗は dispatcher 全体の失敗として raise する。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        ctx.state.session_factory.return_value = MagicMock(
            __aenter__=AsyncMock(side_effect=RuntimeError("session factory boom")),
            __aexit__=AsyncMock(return_value=None),
        )

        with (
            patch(
                "app.queue.tasks.briefing.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            pytest.raises(RuntimeError, match="session factory boom"),
        ):
            await briefing.dispatch_weekly_briefings(ctx=ctx)


class TestSubtask:
    @pytest.mark.asyncio
    async def test_skips_already_generated_week_and_logs_existing(self) -> None:
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock()

        with (
            _patch_facts(_facts(already_generated=True)),
            patch(
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            capture_logs() as logs,
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        service.execute.assert_not_awaited()
        assert [
            e for e in logs if e.get("event") == "briefing_subtask_skipped_existing"
        ]

    @pytest.mark.asyncio
    async def test_skips_generation_when_no_articles(self) -> None:
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock()

        with (
            _patch_facts(_facts(articles=())),
            patch(
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        service.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invokes_service_with_ready_built_from_facts(self) -> None:
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock(
            return_value=GeneratedBriefing(
                week_start=date(2026, 4, 20),
                category_id=1,
                article_count=1,
            )
        )

        with (
            _patch_facts(_facts()),
            patch(
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ) as service_cls,
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        service.execute.assert_awaited_once_with(
            ReadyForBriefing(
                week_start=date(2026, 4, 20),
                category_id=1,
                category_slug="ai",
                category_name="AI",
                articles=(_ARTICLE,),
            )
        )
        # composition root が broker_briefing 起動時に state へ wire した generator が
        # そのまま service に DI される (Pure DI 経路の不変条件)。
        assert service_cls.call_args.args[1] is ctx.state.briefing_generator

    @pytest.mark.asyncio
    async def test_propagates_service_exception(self) -> None:
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock(side_effect=RuntimeError("LLM down"))

        with (
            _patch_facts(_facts()),
            patch(
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            pytest.raises(RuntimeError, match="LLM down"),
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

    @pytest.mark.asyncio
    async def test_missing_category_raises_for_retry(self) -> None:
        """dispatch 後にカテゴリが消えた場合は失敗として raise し、retry に委ねる。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock()

        with (
            _patch_facts(None),
            patch(
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
            pytest.raises(ValueError, match="Category not found"),
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        service.execute.assert_not_awaited()


class TestGenerateBriefingForCategoryStageSpan:
    """``generate_briefing_for_category`` の pipeline_stage span 配線テスト。"""

    @pytest.mark.asyncio
    async def test_span_stage_and_op(self, capfire: CaptureLogfire) -> None:
        """正常系: stage=briefing / op=generate_briefing_for_category が開く。"""
        from app.queue.tasks import briefing

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock(
            return_value=GeneratedBriefing(
                week_start=date(2026, 4, 20),
                category_id=1,
                article_count=5,
            )
        )

        with (
            _patch_facts(_facts()),
            patch(
                "app.queue.tasks.briefing.WeeklyBriefingService",
                return_value=service,
            ),
        ):
            await briefing.generate_briefing_for_category(
                BriefingTaskInput(week_start=date(2026, 4, 20), category_id=1),
                ctx=ctx,
            )

        attrs = pipeline_stage_attrs(capfire)
        assert attrs["stage"] == "briefing"
        assert attrs["op"] == "generate_briefing_for_category"
