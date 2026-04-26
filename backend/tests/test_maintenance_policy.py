"""BackfillWindow / utc_now のユニットテスト。"""

from datetime import UTC, datetime, timedelta

from app.maintenance.policy import BackfillWindow, utc_now


def test_utc_now_returns_timezone_aware_utc() -> None:
    """utc_now は tz-aware (UTC) な datetime を返す。"""
    now = utc_now()
    assert now.tzinfo is UTC


def test_boundaries_at_default_grace_and_window() -> None:
    """BackfillWindow() は (now-30min, now-7days) を返す。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    before, after = BackfillWindow().boundaries_at(now)
    assert before == now - timedelta(minutes=30)
    assert after == now - timedelta(days=7)


def test_boundaries_at_with_custom_durations() -> None:
    """カスタム grace / freshness を尊重する。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    window = BackfillWindow(
        pipeline_grace=timedelta(minutes=15),
        freshness_window=timedelta(days=3),
    )
    before, after = window.boundaries_at(now)
    assert before == now - timedelta(minutes=15)
    assert after == now - timedelta(days=3)
