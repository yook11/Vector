"""週次 briefing の週の計算 (JST・月曜始まり)。

trend_discovery の日次の rolling 窓とは生成の単位が異なるため、briefing で別に持つ。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEK_TZ = "Asia/Tokyo"
_WEEK = timedelta(days=7)


def latest_completed_week_start(now: datetime) -> date:
    """``now`` (JST 想定の tz-aware datetime) における直近完了週の月曜日。

    例: JST 2026-04-27 (月) 00:05 → 2026-04-20 (= 前週月曜)
        JST 2026-04-26 (日) 23:50 → 2026-04-13 (= 完了済み週の月曜)
        JST 2026-04-22 (水) 12:00 → 2026-04-13 (= 前週月曜)
    """
    today = now.date()
    days_since_monday = today.weekday()
    current_monday = today - timedelta(days=days_since_monday)
    return current_monday - _WEEK


def now_in_jst() -> datetime:
    return datetime.now(ZoneInfo(WEEK_TZ))
