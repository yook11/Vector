"""Trend Discovery の集計窓終端 (window_end) を算出する純関数。

Trend Discovery BC は rolling 7d daily で集計するため、半開区間
``[window_end - 7d, window_end)`` の上限となる JST 日付を返す。
``WEEK_TZ = "Asia/Tokyo"`` で日付境界を切る。

責務分離:
- ``latest_window_end``: 純関数 (副作用なし、テスト容易)
- ``now_in_jst``: side-effect 入口 (`datetime.now`)。Task / CLI から呼ぶ薄い
  wrapper で、テストでは差し替え or 直接 datetime を渡す経路を取る
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.insights.trend_discovery.config import WEEK_TZ


def latest_window_end(now: datetime) -> date:
    """``now`` (JST tz-aware) における集計窓の終端 = 当日 0:00 JST の date。

    rolling 7d window の半開区間 ``[window_end - 7d, window_end)`` の上限。
    cron 発火が JST 00:05 でも 06:00 でも、その日の 0:00 までを集計対象とする
    ため戻り値は ``now.date()`` で確定する (同日内の実行時刻で結果が変わらない)。
    """
    return now.date()


def now_in_jst() -> datetime:
    """JST の現在時刻 (Task / CLI から呼ぶ side-effect 入口)。"""
    return datetime.now(ZoneInfo(WEEK_TZ))
