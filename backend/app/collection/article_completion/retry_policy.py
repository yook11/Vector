"""completion scrape retry policy の純データと遅延算出。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """retry policy 1 つを表す純データ。"""

    code: str
    """audit ``reason_code`` suffix (例: ``blip`` → ``temporary_will_retry_blip``)."""

    max_attempts: int
    """この attempt 番号以上で exhausted 扱い (policy ごとに異なる)。"""

    delay_minutes_schedule: tuple[float, ...]
    """attempt_count (1-indexed) ごとの遅延分。長さを超えたら末尾を使う。"""

    def next_delay_minutes(self, attempt_count: int) -> float:
        """``attempt_count`` 番目の試行が失敗した直後に使う next delay。"""
        if attempt_count < 1:
            raise ValueError(f"attempt_count must be >= 1, got {attempt_count}")
        if not self.delay_minutes_schedule:
            raise ValueError(f"empty delay_minutes_schedule for policy {self.code!r}")
        idx = min(attempt_count - 1, len(self.delay_minutes_schedule) - 1)
        return self.delay_minutes_schedule[idx]


BLIP_POLICY = RetryPolicy(
    code="blip",
    max_attempts=8,
    delay_minutes_schedule=(0.5, 1.0, 2.0, 5.0, 5.0, 5.0, 5.0, 5.0),
)
OUTAGE_POLICY = RetryPolicy(
    code="outage",
    max_attempts=12,
    delay_minutes_schedule=(
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
    ),
)
RETRY_AFTER_POLICY = RetryPolicy(
    code="retry_after",
    max_attempts=12,
    # server 指示がない場合の fallback。
    delay_minutes_schedule=OUTAGE_POLICY.delay_minutes_schedule,
)
TIMEOUT_POLICY = RetryPolicy(
    code="timeout",
    max_attempts=8,
    delay_minutes_schedule=(2.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0),
)
UNKNOWN_POLICY = RetryPolicy(
    code="unknown",
    max_attempts=6,
    delay_minutes_schedule=(5.0, 15.0, 30.0, 60.0, 60.0, 60.0),
)


MAX_DELAY_MINUTES = 60.0


def effective_delay_minutes(
    policy: RetryPolicy,
    *,
    retry_after_seconds: float | None,
    attempt_count: int,
) -> float:
    """server 指示または policy schedule から次回 retry までの分数を返す。"""
    if retry_after_seconds is not None:
        delay = retry_after_seconds / 60.0
    else:
        delay = policy.next_delay_minutes(attempt_count)
    return min(delay, MAX_DELAY_MINUTES)
