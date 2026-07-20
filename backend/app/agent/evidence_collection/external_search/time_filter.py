"""External search publication期間の純粋な日付解決。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import assert_never
from zoneinfo import ZoneInfo

from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchDateFilter,
    TimeFilterFailureReason,
)
from app.agent.planning.contract import TargetTimeWindow

_PRODUCT_TIMEZONE = ZoneInfo("Asia/Tokyo")
_ONE_DAY = timedelta(days=1)


class ExternalSearchDateFilterResolutionError(Exception):
    """利用不能なpublication期間を閉じたreasonで通知する。"""

    def __init__(self, reason: TimeFilterFailureReason) -> None:
        self.reason = reason
        super().__init__(reason)


def resolve_external_search_date_filter(
    target_time_window: TargetTimeWindow | None,
    *,
    as_of: datetime,
) -> ExternalSearchDateFilter | None:
    """型付き期間をJST基準の半開日付範囲へ解決する。"""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if target_time_window is None:
        return None

    as_of_jst = as_of.astimezone(_PRODUCT_TIMEZONE)
    today = as_of_jst.date()
    tomorrow = today + _ONE_DAY

    match target_time_window.kind:
        case "today":
            start_date, end_date = today, tomorrow
        case "yesterday":
            start_date, end_date = today - _ONE_DAY, today
        case "last_n_days":
            days = target_time_window.days
            if days is None:
                raise ValueError("last_n_days requires days")
            start_date = (
                (as_of.astimezone(UTC) - timedelta(days=days))
                .astimezone(_PRODUCT_TIMEZONE)
                .date()
            )
            end_date = tomorrow
        case "this_week":
            start_date = today - timedelta(days=today.weekday())
            end_date = tomorrow
        case "last_week":
            end_date = today - timedelta(days=today.weekday())
            start_date = end_date - timedelta(days=7)
        case "this_month":
            start_date, end_date = today.replace(day=1), tomorrow
        case "calendar_month":
            year = target_time_window.year
            month = target_time_window.month
            if year is None or month is None:
                raise ValueError("calendar_month requires year and month")
            start_date = date(year, month, 1)
            if start_date > today:
                raise ExternalSearchDateFilterResolutionError("future_calendar_month")
            if start_date.year == today.year and start_date.month == today.month:
                end_date = tomorrow
            else:
                end_date = _first_day_of_next_month(start_date)
        case "date_range":
            explicit_start = target_time_window.start_date
            inclusive_end = target_time_window.end_date_inclusive
            if explicit_start is None or inclusive_end is None:
                raise ValueError("date_range requires both dates")
            if explicit_start > today:
                raise ExternalSearchDateFilterResolutionError("future_date_range")
            start_date = explicit_start
            end_date = min(inclusive_end + _ONE_DAY, tomorrow)
        case "unsupported_explicit_window":
            raise ExternalSearchDateFilterResolutionError("unsupported_explicit_window")
        case _ as unreachable:
            assert_never(unreachable)

    if start_date == date.min:
        raise ExternalSearchDateFilterResolutionError("unexpandable_start_date")
    return ExternalSearchDateFilter(start_date=start_date, end_date=end_date)


def _first_day_of_next_month(month_start: date) -> date:
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)
