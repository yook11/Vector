"""外部取得を再試行できるまで待つ時刻を表す。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class RetryAt:
    """配送方式によらず、元の待機時刻をUTCで保持する。"""

    value: datetime

    def __post_init__(self) -> None:
        if self.value.utcoffset() is None:
            raise ValueError("retry time must be timezone-aware")
        object.__setattr__(self, "value", self.value.astimezone(UTC))

    def remaining(self, now: datetime) -> timedelta:
        """期限経過後も元の時刻は変えず、残り時間を0にする。"""
        if now.utcoffset() is None:
            raise ValueError("current time must be timezone-aware")
        return max(self.value - now.astimezone(UTC), timedelta())
