"""予定回とソースから、再送しても変わらない取得依頼を構築する。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict

from app.collection.sources.fetch_cadence import FetchCadence


def _normalize_scheduled_at(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("scheduled_at requires a timezone")
    normalized = value.astimezone(UTC)
    if value.microsecond or normalized.microsecond:
        raise ValueError("scheduled_at requires whole seconds")
    return normalized


def _parse_scheduled_at(value: object) -> datetime:
    if isinstance(value, str):
        # datetimeの解析でマイクロ秒未満が切り捨てられる前に拒否する。
        if any(
            digit != "0"
            for fraction in re.findall(r"[.,](\d+)", value)
            for digit in fraction
        ):
            raise ValueError("scheduled_at requires whole seconds")
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("scheduled_at requires an ISO datetime or datetime")
    return _normalize_scheduled_at(value)


ScheduledAt = Annotated[datetime, BeforeValidator(_parse_scheduled_at)]


def build_acquisition_request_id(
    cadence: FetchCadence, scheduled_at: datetime, source_id: int
) -> str:
    """取得依頼の同一性をcadence・UTC予定時刻・ソースIDで表す。"""
    if not isinstance(cadence, FetchCadence):
        raise ValueError("cadence requires FetchCadence")
    if type(source_id) is not int or source_id <= 0:
        raise ValueError("source_id requires a positive integer")
    scheduled_at = _normalize_scheduled_at(scheduled_at)
    timestamp = scheduled_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    return f"{cadence.value}/{timestamp}/{source_id}"


class SourceAcquisitionSchedule(BaseModel):
    """ニュース取得の頻度と、1回分の予定時刻を保持する。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    cadence: FetchCadence
    scheduled_at: ScheduledAt

    def create_request(self, source_id: int) -> SourceAcquisitionRequest:
        return SourceAcquisitionRequest(schedule=self, source_id=source_id)


@dataclass(frozen=True, slots=True)
class SourceAcquisitionRequest:
    """生成時に確定したIDと、対象ソース・予定回を保持する。"""

    schedule: SourceAcquisitionSchedule
    source_id: int
    request_id: str = field(init=False)

    def __post_init__(self) -> None:
        request_id = build_acquisition_request_id(
            self.schedule.cadence, self.schedule.scheduled_at, self.source_id
        )
        object.__setattr__(self, "request_id", request_id)

    def to_message(self) -> dict[str, str | int]:
        """予定回を展開し、取得依頼の送信項目を返す。"""
        return {
            **self.schedule.model_dump(mode="json"),
            "source_id": self.source_id,
            "request_id": self.request_id,
        }
