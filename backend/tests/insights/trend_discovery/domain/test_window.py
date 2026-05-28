"""``app/insights/trend_discovery/domain/window.py`` 純関数のテスト。

``latest_window_end`` は cron / CLI / Service のいずれからも参照される
集計窓終端算出の唯一の真実 (single source of truth)。決定的な入力 → 出力で
test できる。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.insights.trend_discovery.domain.window import latest_window_end

JST = ZoneInfo("Asia/Tokyo")


class TestLatestWindowEnd:
    def test_cron_firing_time_returns_today(self) -> None:
        """cron 発火時刻 (JST 00:05) → その日の date を返す。"""
        now = datetime(2026, 5, 3, 0, 5, tzinfo=JST)
        assert latest_window_end(now) == date(2026, 5, 3)

    def test_monday_returns_today(self) -> None:
        now = datetime(2026, 4, 27, 0, 5, tzinfo=JST)
        assert latest_window_end(now) == date(2026, 4, 27)

    def test_wednesday_returns_today(self) -> None:
        now = datetime(2026, 4, 22, 12, 0, tzinfo=JST)
        assert latest_window_end(now) == date(2026, 4, 22)

    def test_sunday_late_returns_same_day(self) -> None:
        """JST 同日内であれば実行時刻に関わらず同じ日付を返す。"""
        now = datetime(2026, 4, 26, 23, 50, tzinfo=JST)
        assert latest_window_end(now) == date(2026, 4, 26)

    def test_saturday_returns_today(self) -> None:
        now = datetime(2026, 4, 25, 23, 59, tzinfo=JST)
        assert latest_window_end(now) == date(2026, 4, 25)
