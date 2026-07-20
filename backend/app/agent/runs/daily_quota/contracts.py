"""ユーザー日次利用枠の契約。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class DailyRequestLimitExceededError(Exception):
    """ユーザーの日次research request予約枠が上限に達した。"""

    def __init__(
        self,
        *,
        usage_date: date,
        observed_at: datetime,
        decided_at: datetime,
        limit: int,
    ) -> None:
        super().__init__("Daily research request limit exceeded")
        self.usage_date = usage_date
        self.observed_at = observed_at
        self.decided_at = decided_at
        self.limit = limit


class DailyQuotaReleaseOutcome(StrEnum):
    RELEASED = "released"
    NOT_ELIGIBLE = "not_eligible"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class DailyQuotaReservation:
    usage_date: date
    used_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.used_count, int)
            or isinstance(self.used_count, bool)
            or self.used_count < 1
        ):
            raise ValueError("daily quota reservation requires a positive used count")
