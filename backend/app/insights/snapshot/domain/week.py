"""週次 digest の week_start 算出 — JST 月曜起点の純関数。

Service / Task / CLI のいずれもここから import する。Service の static method
としてではなく独立 module に切り出すのは、Service を生成しない経路 (Task /
CLI が Ready を構築する段階) からも使うため。

責務分離:
- ``latest_completed_week_start``: 純関数 (副作用なし、テスト容易)
- ``now_in_jst``: side-effect 入口 (`datetime.now`)。Task / CLI から呼ぶ薄い
  wrapper で、テストでは差し替え or 直接 datetime を渡す経路を取る
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.insights.snapshot.config import WEEK_TZ

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
    """JST の現在時刻 (Task / CLI から呼ぶ side-effect 入口)。"""
    return datetime.now(ZoneInfo(WEEK_TZ))
