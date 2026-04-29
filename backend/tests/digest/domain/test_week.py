"""``app/digest/domain/week.py`` 純関数のテスト。

``latest_completed_week_start`` は cron / CLI / Service のいずれからも参照される
週境界算出の唯一の真実 (single source of truth)。決定的な入力 → 出力で test できる。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.digest.domain.week import latest_completed_week_start

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
        """日曜の深夜は今いる週の月曜の前週 (= 2 週前月曜) を返す。

        日曜 ``weekday() == 6``。同じ週の月曜は 6 日前 (4/20)、その前週月曜が 4/13。
        """
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
