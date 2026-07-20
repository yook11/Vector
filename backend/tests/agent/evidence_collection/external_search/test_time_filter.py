"""External search date-filter value resolution contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from importlib import import_module
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest


def _time_filter_module() -> ModuleType:
    module_name = "app.agent.evidence_collection.external_search.time_filter"
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            pytest.fail(f"S1 contract module is missing: {module_name}")
        raise


def _required_attribute(module: ModuleType, name: str) -> Any:
    value = getattr(module, name, None)
    if value is None:
        pytest.fail(f"S1 contract is missing: {module.__name__}.{name}")
    return value


def _target_time_window(**payload: object) -> object:
    planning_contract = import_module("app.agent.planning.contract")
    target_time_window_type = getattr(planning_contract, "TargetTimeWindow", None)
    if target_time_window_type is None:
        pytest.fail("planning contract must define TargetTimeWindow")
    return target_time_window_type.model_validate(payload)


def _resolve(
    target_time_window: object | None,
    *,
    as_of: datetime,
) -> object | None:
    resolver = _required_attribute(
        _time_filter_module(),
        "resolve_external_search_date_filter",
    )
    return resolver(target_time_window, as_of=as_of)


def _resolution_error_type() -> type[Exception]:
    return _required_attribute(
        _time_filter_module(),
        "ExternalSearchDateFilterResolutionError",
    )


_AS_OF = datetime(2026, 7, 12, 0, 30, tzinfo=UTC)


def test_resolver_returns_no_filter_when_publication_window_is_not_requested() -> None:
    assert _resolve(None, as_of=_AS_OF) is None


@pytest.mark.parametrize(
    ("payload", "expected_dates"),
    [
        pytest.param(
            {"kind": "today"},
            (date(2026, 7, 12), date(2026, 7, 13)),
            id="today",
        ),
        pytest.param(
            {"kind": "yesterday"},
            (date(2026, 7, 11), date(2026, 7, 12)),
            id="yesterday",
        ),
        pytest.param(
            {"kind": "this_week"},
            (date(2026, 7, 6), date(2026, 7, 13)),
            id="this-week",
        ),
        pytest.param(
            {"kind": "last_week"},
            (date(2026, 6, 29), date(2026, 7, 6)),
            id="last-week",
        ),
        pytest.param(
            {"kind": "this_month"},
            (date(2026, 7, 1), date(2026, 7, 13)),
            id="this-month",
        ),
    ],
)
def test_resolver_uses_jst_calendar_boundaries_for_relative_calendar_windows(
    payload: dict[str, object],
    expected_dates: tuple[date, date],
) -> None:
    resolved = _resolve(_target_time_window(**payload), as_of=_AS_OF)

    assert (resolved.start_date, resolved.end_date) == expected_dates


@pytest.mark.parametrize(
    ("days", "expected_dates"),
    [
        pytest.param(1, (date(2026, 7, 11), date(2026, 7, 13)), id="one-day"),
        pytest.param(3, (date(2026, 7, 9), date(2026, 7, 13)), id="three-days"),
        pytest.param(7, (date(2026, 7, 5), date(2026, 7, 13)), id="seven-days"),
        pytest.param(30, (date(2026, 6, 12), date(2026, 7, 13)), id="thirty-days"),
        pytest.param(60, (date(2026, 5, 13), date(2026, 7, 13)), id="sixty-days"),
    ],
)
def test_resolver_projects_last_n_days_from_the_as_of_instant_to_jst_dates(
    days: int,
    expected_dates: tuple[date, date],
) -> None:
    resolved = _resolve(
        _target_time_window(kind="last_n_days", days=days),
        as_of=_AS_OF,
    )

    assert (resolved.start_date, resolved.end_date) == expected_dates


@pytest.mark.parametrize(
    ("as_of", "expected_dates"),
    [
        pytest.param(
            datetime(2026, 3, 12, 10, 30, tzinfo=ZoneInfo("America/New_York")),
            (date(2026, 3, 5), date(2026, 3, 13)),
            id="spring-forward",
        ),
        pytest.param(
            datetime(2026, 11, 5, 10, 30, tzinfo=ZoneInfo("America/New_York")),
            (date(2026, 10, 30), date(2026, 11, 7)),
            id="fall-back",
        ),
    ],
)
def test_resolver_subtracts_last_n_days_from_the_utc_instant_across_dst(
    as_of: datetime,
    expected_dates: tuple[date, date],
) -> None:
    resolved = _resolve(
        _target_time_window(kind="last_n_days", days=7),
        as_of=as_of,
    )

    assert (resolved.start_date, resolved.end_date) == expected_dates


@pytest.mark.parametrize(
    ("as_of", "payload", "expected_dates"),
    [
        pytest.param(
            _AS_OF,
            {"kind": "calendar_month", "year": 2026, "month": 6},
            (date(2026, 6, 1), date(2026, 7, 1)),
            id="past-month",
        ),
        pytest.param(
            _AS_OF,
            {"kind": "calendar_month", "year": 2026, "month": 7},
            (date(2026, 7, 1), date(2026, 7, 13)),
            id="current-month-clipped",
        ),
        pytest.param(
            datetime(2026, 1, 4, tzinfo=UTC),
            {"kind": "calendar_month", "year": 2025, "month": 12},
            (date(2025, 12, 1), date(2026, 1, 1)),
            id="december-rollover",
        ),
        pytest.param(
            _AS_OF,
            {"kind": "calendar_month", "year": 2024, "month": 2},
            (date(2024, 2, 1), date(2024, 3, 1)),
            id="leap-year-february",
        ),
    ],
)
def test_resolver_handles_calendar_month_boundaries(
    as_of: datetime,
    payload: dict[str, object],
    expected_dates: tuple[date, date],
) -> None:
    resolved = _resolve(_target_time_window(**payload), as_of=as_of)

    assert (resolved.start_date, resolved.end_date) == expected_dates


@pytest.mark.parametrize(
    ("payload", "expected_dates"),
    [
        pytest.param(
            {"kind": "this_month"},
            (date(2026, 8, 1), date(2026, 8, 2)),
            id="this-month-at-jst-midnight-boundary",
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 7},
            (date(2026, 7, 1), date(2026, 8, 1)),
            id="july-is-complete-past-month",
        ),
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 8},
            (date(2026, 8, 1), date(2026, 8, 2)),
            id="august-is-current-month-not-future",
        ),
    ],
)
def test_resolver_uses_jst_not_utc_at_calendar_month_boundary(
    payload: dict[str, object],
    expected_dates: tuple[date, date],
) -> None:
    resolved = _resolve(
        _target_time_window(**payload),
        as_of=datetime(2026, 7, 31, 15, 30, tzinfo=UTC),
    )

    assert (resolved.start_date, resolved.end_date) == expected_dates


@pytest.mark.parametrize(
    ("payload", "expected_dates"),
    [
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-06-01",
                "end_date_inclusive": "2026-06-15",
            },
            (date(2026, 6, 1), date(2026, 6, 16)),
            id="inclusive-range-to-half-open",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-06-01",
                "end_date_inclusive": "2026-06-01",
            },
            (date(2026, 6, 1), date(2026, 6, 2)),
            id="single-day",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-07-01",
                "end_date_inclusive": "2026-07-31",
            },
            (date(2026, 7, 1), date(2026, 7, 13)),
            id="future-end-clipped-to-as-of-day",
        ),
    ],
)
def test_resolver_converts_inclusive_date_ranges_to_half_open_filters(
    payload: dict[str, object],
    expected_dates: tuple[date, date],
) -> None:
    resolved = _resolve(_target_time_window(**payload), as_of=_AS_OF)

    assert (resolved.start_date, resolved.end_date) == expected_dates


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 8},
            "future_calendar_month",
            id="future-calendar-month",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-07-13",
                "end_date_inclusive": "2026-07-13",
            },
            "future_date_range",
            id="future-date-range",
        ),
        pytest.param(
            {"kind": "unsupported_explicit_window"},
            "unsupported_explicit_window",
            id="unsupported-explicit-window",
        ),
    ],
)
def test_resolver_fails_closed_with_a_typed_reason_for_unapplicable_windows(
    payload: dict[str, object],
    reason: str,
) -> None:
    resolution_error_type = _resolution_error_type()

    with pytest.raises(resolution_error_type) as raised:
        _resolve(_target_time_window(**payload), as_of=_AS_OF)

    assert raised.value.reason == reason


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"kind": "calendar_month", "year": 1, "month": 1},
            id="calendar-month-at-date-min",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "0001-01-01",
                "end_date_inclusive": "0001-01-01",
            },
            id="date-range-at-date-min",
        ),
    ],
)
def test_resolver_fails_closed_when_filter_start_cannot_expand_for_provider(
    payload: dict[str, object],
) -> None:
    resolution_error_type = _resolution_error_type()

    with pytest.raises(resolution_error_type) as raised:
        _resolve(_target_time_window(**payload), as_of=_AS_OF)

    assert raised.value.reason == "unexpandable_start_date"


@pytest.mark.parametrize(
    ("payload", "forbidden_fragments"),
    [
        pytest.param(
            {"kind": "calendar_month", "year": 2026, "month": 8},
            ("2026", "08", "01"),
            id="future-calendar-month",
        ),
        pytest.param(
            {
                "kind": "date_range",
                "start_date": "2026-07-13",
                "end_date_inclusive": "2026-07-13",
            },
            ("2026", "07", "13"),
            id="future-date-range",
        ),
    ],
)
def test_resolution_error_does_not_expose_window_dates(
    payload: dict[str, object],
    forbidden_fragments: tuple[str, ...],
) -> None:
    resolution_error_type = _resolution_error_type()

    with pytest.raises(resolution_error_type) as raised:
        _resolve(_target_time_window(**payload), as_of=_AS_OF)

    serialized_error = " ".join(
        (str(raised.value), repr(raised.value), repr(raised.value.args))
    )
    assert not any(fragment in serialized_error for fragment in forbidden_fragments)


def test_resolver_propagates_naive_as_of_as_a_programming_error() -> None:
    resolution_error_type = _resolution_error_type()

    with pytest.raises(ValueError) as raised:
        _resolve(
            _target_time_window(kind="today"),
            as_of=datetime(2026, 7, 12, 9, 30),
        )

    assert not isinstance(raised.value, resolution_error_type)
