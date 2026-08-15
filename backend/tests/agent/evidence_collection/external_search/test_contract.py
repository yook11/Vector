"""External search の query-collection に閉じた契約。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.agent.evidence_collection.external_search import (
    ExternalQueryGenerationInput,
    ExternalSearchDateFilter,
)
from app.agent.planning.contract import TargetTimeWindow


def test_external_search_date_filter_is_a_frozen_half_open_value_object() -> None:
    date_filter = ExternalSearchDateFilter(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )

    assert (date_filter.start_date, date_filter.end_date) == (
        date(2026, 6, 1),
        date(2026, 6, 2),
    )


def test_external_search_date_filter_is_frozen() -> None:
    date_filter = ExternalSearchDateFilter(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )

    with pytest.raises(ValidationError):
        date_filter.end_date = date(2026, 6, 3)


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        pytest.param(date(2026, 6, 1), date(2026, 6, 1), id="same-day"),
        pytest.param(date(2026, 6, 2), date(2026, 6, 1), id="reverse-order"),
    ],
)
def test_external_search_date_filter_rejects_non_half_open_ranges(
    start_date: date,
    end_date: date,
) -> None:
    with pytest.raises(ValidationError):
        ExternalSearchDateFilter(start_date=start_date, end_date=end_date)


def test_external_search_date_filter_rejects_start_that_cannot_expand_one_day() -> None:
    with pytest.raises(ValidationError):
        ExternalSearchDateFilter(
            start_date=date.min,
            end_date=date.min + timedelta(days=1),
        )


def test_external_query_generation_input_uses_typed_time_window() -> None:
    hints = get_type_hints(ExternalQueryGenerationInput)

    assert hints["target_time_window"] == TargetTimeWindow | None
