"""配信対象の選択・lease期間・再試行待ち時間の成立条件を保証する。"""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryBatchSelection:
    """対象イベント種別と件数を指定し、DB上の配信状態はRepositoryで判定する。"""

    event_type: str
    limit: int

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str):
            raise TypeError("event_type must be a string")
        if not self.event_type.strip():
            raise ValueError("event_type must not be blank")
        if type(self.limit) is not int:
            raise TypeError("limit must be an integer")


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
