"""lease期間・再試行待ち時間の成立条件を保証する。"""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class LeaseDuration:
    """配信担当を確保する期間を、正の時間として保持する。"""

    value: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.value, timedelta):
            raise TypeError("lease_duration must be a timedelta")
        if self.value <= timedelta(0):
            raise ValueError("lease_duration must be positive")


@dataclass(frozen=True, slots=True)
class RetryDelay:
    """次の送信試行までの待ち時間を、ゼロ以上の時間として保持する。"""

    value: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.value, timedelta):
            raise TypeError("retry_delay must be a timedelta")
        if self.value < timedelta(0):
            raise ValueError("retry_delay must not be negative")
