"""briefing の発行周期 (JST 月曜始まりの週) のルールのテスト。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.insights.briefing.domain.briefing import (
    latest_completed_week_start,
    week_bounds,
)

JST = ZoneInfo("Asia/Tokyo")


class TestLatestCompletedWeekStart:
    def test_monday_returns_previous_monday(self) -> None:
        """月曜日に呼ぶと前週月曜を返す (今週はまだ未完了)。"""
        now = datetime(2026, 4, 27, 0, 5, tzinfo=JST)
        assert latest_completed_week_start(now) == date(2026, 4, 20)

    def test_cron_firing_time_returns_previous_monday(self) -> None:
        """cron 発火時刻 (JST 月曜 00:05) で前週月曜を返す。"""
        now = datetime(2026, 4, 27, 0, 5, tzinfo=JST)
        assert latest_completed_week_start(now) == date(2026, 4, 20)

    def test_sunday_late_returns_two_weeks_back_monday(self) -> None:
        """日曜の深夜は今いる週の月曜の前週 (= 2 週前月曜) を返す。"""
        now = datetime(2026, 4, 26, 23, 50, tzinfo=JST)
        assert latest_completed_week_start(now) == date(2026, 4, 13)

    def test_wednesday_returns_previous_week_monday(self) -> None:
        """水曜は今週がまだ未完了、前週月曜を返す。"""
        now = datetime(2026, 4, 22, 12, 0, tzinfo=JST)
        assert latest_completed_week_start(now) == date(2026, 4, 13)

    def test_saturday_returns_previous_week_monday(self) -> None:
        """土曜は今週がまだ未完了、前週月曜を返す。"""
        now = datetime(2026, 4, 25, 23, 59, tzinfo=JST)
        assert latest_completed_week_start(now) == date(2026, 4, 13)


class TestWeekBounds:
    def test_covers_monday_to_next_monday_in_jst(self) -> None:
        """号の対象期間は JST の月曜 0:00 から翌月曜 0:00 まで (UTC では前日 15:00)。"""
        assert week_bounds(date(2026, 4, 20)) == (
            datetime(2026, 4, 19, 15, 0, tzinfo=UTC),
            datetime(2026, 4, 26, 15, 0, tzinfo=UTC),
        )
