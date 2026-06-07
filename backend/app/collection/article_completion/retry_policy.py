"""completion scrape retry の遅延原始型と再投入テンプレート。

``ScheduleDelay`` / ``FixedDelay`` は汎用の遅延スケジューリング原始型 (誰が次回
``ready_at`` を決めるかの sum 型)。``RetrySchedule`` は cap と既定 delay を束ねた
名前付きテンプレートで、``scrape_failure.py`` が origin error を分類する際に
``ScrapeRetryable`` を組み立てる材料になる。
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_DELAY_MINUTES = 60.0


@dataclass(frozen=True, slots=True)
class ScheduleDelay:
    """attempt_count (1-indexed) ごとに段階的に伸ばす backoff 遅延。"""

    minutes_schedule: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.minutes_schedule:
            raise ValueError("minutes_schedule must not be empty")

    def minutes(self, attempt_count: int) -> float:
        """``attempt_count`` 番目の失敗直後に使う next delay (cap 適用済)。"""
        if attempt_count < 1:
            raise ValueError(f"attempt_count must be >= 1, got {attempt_count}")
        idx = min(attempt_count - 1, len(self.minutes_schedule) - 1)
        return min(self.minutes_schedule[idx], MAX_DELAY_MINUTES)


@dataclass(frozen=True, slots=True)
class FixedDelay:
    """server の ``Retry-After`` 指示。attempt_count に依らず固定 (cap 適用済)。"""

    seconds: float

    def minutes(self, attempt_count: int) -> float:
        return min(self.seconds / 60.0, MAX_DELAY_MINUTES)


RetryDelay = ScheduleDelay | FixedDelay
"""次回 ``ready_at`` を決める遅延の sum 型 (schedule 駆動 / server 指示)。"""


@dataclass(frozen=True, slots=True)
class RetrySchedule:
    """cap (max_attempts) と既定 delay を束ねた再投入テンプレート。"""

    max_attempts: int
    delay: ScheduleDelay


BLIP = RetrySchedule(8, ScheduleDelay((0.5, 1.0, 2.0, 5.0, 5.0, 5.0, 5.0, 5.0)))
OUTAGE = RetrySchedule(
    12,
    ScheduleDelay(
        (
            5.0,
            15.0,
            30.0,
            60.0,
            60.0,
            60.0,
            60.0,
            60.0,
            60.0,
            60.0,
            60.0,
            60.0,
        )
    ),
)
TIMEOUT = RetrySchedule(8, ScheduleDelay((2.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0)))
UNKNOWN = RetrySchedule(6, ScheduleDelay((5.0, 15.0, 30.0, 60.0, 60.0, 60.0)))
