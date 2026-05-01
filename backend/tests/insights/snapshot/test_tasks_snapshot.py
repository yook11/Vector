"""generate_weekly_snapshot タスクのテスト (Phase 4)。

検証する観点:
- ``schedule`` ラベルに JST 月曜 00:05 相当の cron (= UTC 日曜 15:05) が登録される
- ctx.state.session_factory + Ready 構築 + Service.execute(ready) の dispatch
- ``ReadyForDigest.try_advance_from`` が None を返したら Service を呼ばずに
  early return する
- Service が例外を上げた場合は捕まえずに伝播する (failure_visibility 原則)
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.insights.snapshot.application.snapshot import Generated
from app.insights.snapshot.domain.ready import ReadyForDigest

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


# ---------------------------------------------------------------------------
# schedule ラベル
# ---------------------------------------------------------------------------


class TestSchedule:
    def test_cron_matches_jst_monday_midnight(self) -> None:
        """UTC 日曜 15:05 = JST 月曜 00:05 の cron 文字列が登録されている。"""
        from app.insights.snapshot.tasks import snapshot

        schedule = snapshot.generate_weekly_snapshot.labels.get("schedule")
        assert isinstance(schedule, list)
        assert any(entry.get("cron") == "5 15 * * 0" for entry in schedule)


# ---------------------------------------------------------------------------
# 本体 dispatch
# ---------------------------------------------------------------------------


class TestRun:
    @pytest.mark.asyncio
    async def test_invokes_service_with_ready(self) -> None:
        """try_advance_from で Ready が返ったら Service.execute(ready) を呼ぶ。"""
        from app.insights.snapshot.tasks import snapshot

        ctx = _ctx_with_session_factory()
        target_week = date(2026, 4, 20)
        ready = ReadyForDigest(week_start=target_week, force=False)

        service = MagicMock()
        service.execute = AsyncMock(
            return_value=Generated(week_start=target_week, source_analysis_count=42)
        )

        with (
            patch(
                "app.insights.snapshot.tasks.snapshot.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForDigest, "try_advance_from", new=AsyncMock(return_value=ready)
            ),
            patch(
                "app.insights.snapshot.tasks.snapshot.WeeklyTrendsSnapshotService",
                return_value=service,
            ) as service_cls,
        ):
            await snapshot.generate_weekly_snapshot(ctx=ctx)

        service_cls.assert_called_once_with(ctx.state.session_factory)
        service.execute.assert_awaited_once_with(ready)

    @pytest.mark.asyncio
    async def test_skips_service_when_ready_is_none(self) -> None:
        """try_advance_from が None を返したら Service.execute は呼ばれない。"""
        from app.insights.snapshot.tasks import snapshot

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.execute = AsyncMock()

        with (
            patch(
                "app.insights.snapshot.tasks.snapshot.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForDigest, "try_advance_from", new=AsyncMock(return_value=None)
            ),
            patch(
                "app.insights.snapshot.tasks.snapshot.WeeklyTrendsSnapshotService",
                return_value=service,
            ),
        ):
            await snapshot.generate_weekly_snapshot(ctx=ctx)

        service.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_propagates_service_exception(self) -> None:
        """Service が例外を上げたら捕まえず再 raise する (failure_visibility)。"""
        from app.insights.snapshot.tasks import snapshot

        ctx = _ctx_with_session_factory()
        ready = ReadyForDigest(week_start=date(2026, 4, 20), force=False)

        service = MagicMock()
        service.execute = AsyncMock(side_effect=RuntimeError("aggregation failed"))

        with (
            patch(
                "app.insights.snapshot.tasks.snapshot.now_in_jst",
                return_value=datetime(2026, 4, 27, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForDigest, "try_advance_from", new=AsyncMock(return_value=ready)
            ),
            patch(
                "app.insights.snapshot.tasks.snapshot.WeeklyTrendsSnapshotService",
                return_value=service,
            ),
            pytest.raises(RuntimeError, match="aggregation failed"),
        ):
            await snapshot.generate_weekly_snapshot(ctx=ctx)
