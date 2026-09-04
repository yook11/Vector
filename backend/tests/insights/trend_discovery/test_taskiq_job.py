"""run_trend_discovery Taskiq jobのテスト + pipeline_stage span 配線テスト。

検証する観点:
- ``schedule`` ラベルに JST 毎日 00:05 相当の cron (= UTC 毎日 15:05) が登録される
- ctx.state.session_factoryからService.create()を呼ぶ
- ``ReadyForTrendDiscovery.try_advance_from`` が None を返したら Service を呼ばずに
  early return する
- Service が例外を上げた場合は捕まえずに伝播する (failure_visibility 原則)
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs
from taskiq import InMemoryBroker

from app.audit.domain.event import EventType, Stage
from app.audit.stages.trend_discovery import TrendDiscoveryOutcomeCode
from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery
from app.insights.trend_discovery.service import (
    TRENDS_REVALIDATE_TAGS,
    SkippedNoTargetArticles,
    TrendDiscoveryCompleted,
    TrendDiscoveryConflict,
    TrendDiscoveryService,
)
from tests.logfire._span_helpers import pipeline_stage_attrs

JST = ZoneInfo("Asia/Tokyo")


def _ctx_with_session_factory() -> MagicMock:
    """taskiq Context の最小 fake。``async with session_factory()`` を fake する。"""
    ctx = MagicMock()

    session = MagicMock()
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=session_ctx)
    ctx.state.session_factory = factory
    return ctx


class TestSchedule:
    def test_cron_matches_jst_daily_midnight(self) -> None:
        """指定brokerへ既存のtask metadataで登録される。"""
        from app.insights.trend_discovery import taskiq_job as trend_discovery

        broker = InMemoryBroker()
        task = trend_discovery.register_trend_discovery_task(broker)
        schedule = task.labels.get("schedule")
        assert task.task_name == "run_trend_discovery"
        assert task.labels["timeout"] == 600
        assert task.labels["max_retries"] == 0
        assert task.labels["retry_on_error"] is False
        assert isinstance(schedule, list)
        assert any(entry.get("cron") == "5 15 * * *" for entry in schedule)
        assert task.broker is broker


class TestRun:
    @pytest.mark.asyncio
    async def test_invokes_service_with_ready(self) -> None:
        """try_advance_from で Ready が返ったら Service.execute(ready) を呼ぶ。"""
        from app.insights.trend_discovery import taskiq_job as trend_discovery

        ctx = _ctx_with_session_factory()
        target_window_end = date(2026, 5, 3)
        ready = ReadyForTrendDiscovery(window_end=target_window_end, force=False)

        service = MagicMock()
        service.execute = AsyncMock(
            return_value=TrendDiscoveryCompleted(
                window_end=target_window_end,
                source_analysis_count=42,
                completed_category_count=3,
            )
        )
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=None)

        with (
            patch(
                "app.insights.trend_discovery.service.now_in_jst",
                return_value=datetime(2026, 5, 3, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForTrendDiscovery,
                "try_advance_from",
                new=AsyncMock(return_value=ready),
            ),
            patch.object(TrendDiscoveryService, "execute", new=service.execute),
            patch(
                "app.insights.trend_discovery.taskiq_job.FrontendRevalidateNotifier.from_settings",
                return_value=notifier,
            ),
            patch(
                "app.insights.trend_discovery.service.append_trend_discovery_run_event_best_effort",
                new=AsyncMock(),
            ) as audit,
        ):
            await trend_discovery.run_trend_discovery(ctx=ctx)

        service.execute.assert_awaited_once_with(ready)
        # 生成成功で frontend revalidate を 1 回打つ。
        notifier.notify.assert_awaited_once_with(tags=TRENDS_REVALIDATE_TAGS)
        audit.assert_awaited_once()
        assert audit.await_args.args == (ctx.state.session_factory,)
        assert audit.await_args.kwargs["event_type"] == EventType.SUCCEEDED
        assert (
            audit.await_args.kwargs["outcome_code"]
            == TrendDiscoveryOutcomeCode.RUN_COMPLETED
        )
        assert audit.await_args.kwargs["trigger"] == "cron"
        assert audit.await_args.kwargs["requested_update"] is False
        assert audit.await_args.kwargs["source_analysis_count"] == 42
        assert audit.await_args.kwargs["completed_category_count"] == 3

    @pytest.mark.asyncio
    async def test_skips_service_when_ready_is_none(self) -> None:
        """try_advance_from が None を返したら Service.execute は呼ばれない。"""
        from app.insights.trend_discovery import taskiq_job as trend_discovery

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock()

        with (
            patch(
                "app.insights.trend_discovery.service.now_in_jst",
                return_value=datetime(2026, 5, 3, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForTrendDiscovery,
                "try_advance_from",
                new=AsyncMock(return_value=None),
            ),
            patch.object(TrendDiscoveryService, "execute", new=service.execute),
            patch(
                "app.insights.trend_discovery.service.append_trend_discovery_run_event_best_effort",
                new=AsyncMock(),
            ) as audit,
            capture_logs() as logs,
        ):
            await trend_discovery.run_trend_discovery(ctx=ctx)

        service.execute.assert_not_awaited()
        # 既存 snapshot skip は監査に焼かず log で観測する。
        audit.assert_not_awaited()
        assert [
            e
            for e in logs
            if e.get("event") == "trend_discovery_task_skipped_already_exists"
        ]

    @pytest.mark.asyncio
    async def test_skips_when_service_reports_no_target_articles(self) -> None:
        """Service が対象記事 0 件を返したら正常 skip として終了する。"""
        from app.insights.trend_discovery import taskiq_job as trend_discovery

        ctx = _ctx_with_session_factory()
        target_window_end = date(2026, 5, 3)
        ready = ReadyForTrendDiscovery(window_end=target_window_end, force=False)

        service = MagicMock()
        service.execute = AsyncMock(
            return_value=SkippedNoTargetArticles(window_end=target_window_end)
        )
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=None)

        with (
            patch(
                "app.insights.trend_discovery.service.now_in_jst",
                return_value=datetime(2026, 5, 3, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForTrendDiscovery,
                "try_advance_from",
                new=AsyncMock(return_value=ready),
            ),
            patch.object(TrendDiscoveryService, "execute", new=service.execute),
            patch(
                "app.insights.trend_discovery.taskiq_job.FrontendRevalidateNotifier.from_settings",
                return_value=notifier,
            ),
            patch(
                "app.insights.trend_discovery.service.append_trend_discovery_run_event_best_effort",
                new=AsyncMock(),
            ) as audit,
            capture_logs() as logs,
        ):
            await trend_discovery.run_trend_discovery(ctx=ctx)

        service.execute.assert_awaited_once_with(ready)
        # 生成していないので revalidate は打たない。
        notifier.notify.assert_not_awaited()
        # 対象 0 件 skip は監査に焼かず log で観測する。
        audit.assert_not_awaited()
        assert [
            e
            for e in logs
            if e.get("event") == "trend_discovery_task_skipped_no_target_articles"
        ]

    @pytest.mark.asyncio
    async def test_skips_when_service_reports_conflict(self) -> None:
        """Service が同時書き込み conflict を返したら正常 skip として監査する。"""
        from app.insights.trend_discovery import taskiq_job as trend_discovery

        ctx = _ctx_with_session_factory()
        target_window_end = date(2026, 5, 3)
        ready = ReadyForTrendDiscovery(window_end=target_window_end, force=False)

        service = MagicMock()
        service.execute = AsyncMock(
            return_value=TrendDiscoveryConflict(
                window_end=target_window_end,
                source_analysis_count=42,
                completed_category_count=3,
            )
        )
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=None)

        with (
            patch(
                "app.insights.trend_discovery.service.now_in_jst",
                return_value=datetime(2026, 5, 3, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForTrendDiscovery,
                "try_advance_from",
                new=AsyncMock(return_value=ready),
            ),
            patch.object(TrendDiscoveryService, "execute", new=service.execute),
            patch(
                "app.insights.trend_discovery.taskiq_job.FrontendRevalidateNotifier.from_settings",
                return_value=notifier,
            ),
            patch(
                "app.insights.trend_discovery.service.append_trend_discovery_run_event_best_effort",
                new=AsyncMock(),
            ) as audit,
            capture_logs() as logs,
        ):
            await trend_discovery.run_trend_discovery(ctx=ctx)

        service.execute.assert_awaited_once_with(ready)
        # conflict (別 worker が先に保存) では revalidate を打たない。
        notifier.notify.assert_not_awaited()
        # race 敗北 skip は監査に焼かず log で観測する。
        audit.assert_not_awaited()
        conflict_logs = [
            e for e in logs if e.get("event") == "trend_discovery_task_conflict"
        ]
        assert conflict_logs
        assert conflict_logs[-1]["source_analysis_count"] == 42
        assert conflict_logs[-1]["category_count"] == 3

    @pytest.mark.asyncio
    async def test_propagates_service_exception(self) -> None:
        """Service が例外を上げたら捕まえず再 raise する (failure_visibility)。"""
        from app.insights.trend_discovery import taskiq_job as trend_discovery

        ctx = _ctx_with_session_factory()
        ready = ReadyForTrendDiscovery(window_end=date(2026, 5, 3), force=False)

        service = MagicMock()
        service.execute = AsyncMock(side_effect=RuntimeError("aggregation failed"))

        with (
            patch(
                "app.insights.trend_discovery.service.now_in_jst",
                return_value=datetime(2026, 5, 3, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForTrendDiscovery,
                "try_advance_from",
                new=AsyncMock(return_value=ready),
            ),
            patch.object(TrendDiscoveryService, "execute", new=service.execute),
            patch(
                "app.insights.trend_discovery.service.append_trend_discovery_run_event_best_effort",
                new=AsyncMock(),
            ) as audit,
            pytest.raises(RuntimeError, match="aggregation failed"),
        ):
            await trend_discovery.run_trend_discovery(ctx=ctx)

        audit.assert_awaited_once()
        assert audit.await_args.kwargs["event_type"] == EventType.FAILED
        assert (
            audit.await_args.kwargs["outcome_code"]
            == TrendDiscoveryOutcomeCode.RUN_FAILED
        )
        assert isinstance(audit.await_args.kwargs["exc"], RuntimeError)

    @pytest.mark.asyncio
    async def test_propagates_ready_check_exception_after_audit(self) -> None:
        """Ready 構築時の DB 例外も failed 監査を焼いて再 raise する。"""
        from app.insights.trend_discovery import taskiq_job as trend_discovery

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock()

        with (
            patch(
                "app.insights.trend_discovery.service.now_in_jst",
                return_value=datetime(2026, 5, 3, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForTrendDiscovery,
                "try_advance_from",
                new=AsyncMock(side_effect=RuntimeError("ready failed")),
            ),
            patch.object(TrendDiscoveryService, "execute", new=service.execute),
            patch(
                "app.insights.trend_discovery.service.append_trend_discovery_run_event_best_effort",
                new=AsyncMock(),
            ) as audit,
            pytest.raises(RuntimeError, match="ready failed"),
        ):
            await trend_discovery.run_trend_discovery(ctx=ctx)

        service.execute.assert_not_awaited()
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["event_type"] == EventType.FAILED
        assert (
            audit.await_args.kwargs["outcome_code"]
            == TrendDiscoveryOutcomeCode.RUN_FAILED
        )


class TestRunTrendDiscoveryStageSpan:
    """``run_trend_discovery`` task が pipeline_stage span を正しく開く配線テスト。"""

    @pytest.mark.asyncio
    async def test_span_stage_and_op(self, capfire: CaptureLogfire) -> None:
        """正常系: stage=trend_discovery / op=run_trend_discovery が span に開く。"""
        from app.insights.trend_discovery import taskiq_job as trend_discovery

        ctx = _ctx_with_session_factory()
        target_window_end = date(2026, 5, 3)
        ready = ReadyForTrendDiscovery(window_end=target_window_end, force=False)

        service = MagicMock()
        service.execute = AsyncMock(
            return_value=TrendDiscoveryCompleted(
                window_end=target_window_end,
                source_analysis_count=10,
                completed_category_count=2,
            )
        )
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=None)

        with (
            patch(
                "app.insights.trend_discovery.service.now_in_jst",
                return_value=datetime(2026, 5, 3, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForTrendDiscovery,
                "try_advance_from",
                new=AsyncMock(return_value=ready),
            ),
            patch.object(TrendDiscoveryService, "execute", new=service.execute),
            patch(
                "app.insights.trend_discovery.taskiq_job.FrontendRevalidateNotifier.from_settings",
                return_value=notifier,
            ),
            patch(
                "app.insights.trend_discovery.service.append_trend_discovery_run_event_best_effort",
                new=AsyncMock(),
            ),
        ):
            await trend_discovery.run_trend_discovery(ctx=ctx)

        attrs = pipeline_stage_attrs(capfire)
        assert attrs["stage"] == Stage.TREND_DISCOVERY.value  # == "trend_discovery"
        assert attrs["op"] == "run_trend_discovery"
