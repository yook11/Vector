"""予定回とソースから、再送しても変わらない取得依頼を構築する。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, computed_field

from app.collection.sources.fetch_cadence import FetchCadence

if TYPE_CHECKING:
    from app.collection.sources.dispatch import SourceDispatchTarget


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


class SourceDispatchInput(BaseModel):
    """Schedulerが指定した予定回を保持する。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    cadence: FetchCadence
    scheduled_at: ScheduledAt


class SourceAcquisitionRequest(BaseModel):
    """ソースの取得依頼と、その内容に対応した識別子を保持する。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    cadence: FetchCadence
    scheduled_at: ScheduledAt
    source_id: int = Field(gt=0, strict=True)

    @computed_field
    @property
    def request_id(self) -> str:
        return build_acquisition_request_id(
            self.cadence, self.scheduled_at, self.source_id
        )


def build_acquisition_requests(
    schedule: SourceDispatchInput, sources: tuple[SourceDispatchTarget, ...]
) -> tuple[SourceAcquisitionRequest, ...]:
    """確認済みの対象ソースから、その予定回の取得依頼一覧を作る。"""
    return tuple(
        SourceAcquisitionRequest(
            cadence=schedule.cadence,
            scheduled_at=schedule.scheduled_at,
            source_id=source.id,
        )
        for source in sources
    )
