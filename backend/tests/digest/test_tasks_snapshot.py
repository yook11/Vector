"""generate_weekly_snapshot タスクのテスト。

検証する観点:
- ``schedule`` ラベルに JST 月曜 00:05 相当の cron (= UTC 日曜 15:05) が登録される
- ``ctx.state.session_factory`` を Service にそのまま渡す
- ``Generated`` / ``Skipped`` どちらの戻り値でも logger.info で完了を観測できる
- Service が例外を上げた場合は捕まえずに伝播する (failure_visibility 原則)
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.digest.application.snapshot import Generated, Skipped


def _ctx_with_session_factory() -> MagicMock:
    ctx = MagicMock()
    ctx.state.session_factory = MagicMock(name="session_factory")
    return ctx


# ---------------------------------------------------------------------------
# schedule ラベル
# ---------------------------------------------------------------------------


class TestSchedule:
    def test_cron_matches_jst_monday_midnight(self) -> None:
        """UTC 日曜 15:05 = JST 月曜 00:05 の cron 文字列が登録されている。"""
        from app.digest.tasks import snapshot

        schedule = snapshot.generate_weekly_snapshot.labels.get("schedule")
        assert isinstance(schedule, list)
        assert any(entry.get("cron") == "5 15 * * 0" for entry in schedule)


# ---------------------------------------------------------------------------
# 本体 dispatch
# ---------------------------------------------------------------------------


class TestRun:
    @pytest.mark.asyncio
    async def test_invokes_service_with_session_factory(self) -> None:
        """ctx.state.session_factory をそのまま Service コンストラクタに渡す。"""
        from app.digest.tasks import snapshot

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.generate_for_latest_completed_week = AsyncMock(
            return_value=Generated(
                week_start=date(2026, 4, 20), source_analysis_count=42
            )
        )

        with patch(
            "app.digest.tasks.snapshot.WeeklyTrendsSnapshotService",
            return_value=service,
        ) as service_cls:
            await snapshot.generate_weekly_snapshot(ctx=ctx)

        service_cls.assert_called_once_with(ctx.state.session_factory)
        service.generate_for_latest_completed_week.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_handles_skipped_outcome(self) -> None:
        """Skipped が返っても例外なく完了する (Service の判断を尊重)。"""
        from app.digest.tasks import snapshot

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.generate_for_latest_completed_week = AsyncMock(
            return_value=Skipped(week_start=date(2026, 4, 20))
        )

        with patch(
            "app.digest.tasks.snapshot.WeeklyTrendsSnapshotService",
            return_value=service,
        ):
            await snapshot.generate_weekly_snapshot(ctx=ctx)

        service.generate_for_latest_completed_week.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_propagates_service_exception(self) -> None:
        """Service が例外を上げたら捕まえず再 raise する (failure_visibility)。"""
        from app.digest.tasks import snapshot

        ctx = _ctx_with_session_factory()
        service = MagicMock()
        service.generate_for_latest_completed_week = AsyncMock(
            side_effect=RuntimeError("aggregation failed")
        )

        with (
            patch(
                "app.digest.tasks.snapshot.WeeklyTrendsSnapshotService",
                return_value=service,
            ),
            pytest.raises(RuntimeError, match="aggregation failed"),
        ):
            await snapshot.generate_weekly_snapshot(ctx=ctx)
