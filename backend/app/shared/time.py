"""共有時刻ユーティリティ。

`queue/` / `search/` / その他ドメインから参照される時刻 helper。
テスト時に freezegun 等で差し替えられる窓口として 1 か所に集約する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter


def utc_now() -> datetime:
    """UTC の現在時刻を返す (テスト時に freezegun 等で差し替えられる窓口)。"""
    return datetime.now(UTC)


def elapsed_ms_since(started_at_seconds: float) -> float:
    """perf_counterで取得した開始値からの経過時間をミリ秒で返す。"""
    return (perf_counter() - started_at_seconds) * 1000
