"""再送の同一性と、時刻の補完・丸めによる誤識別を検証する。"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.collection.sources.acquisition_request import (
    SourceDispatchInput,
    build_acquisition_request_id,
)
from app.collection.sources.fetch_cadence import FetchCadence


def test_request_identity_uses_the_scheduled_instant_and_source():
    """時差表記だけの違いは同じ依頼となり、別の予定回・頻度・ソースは区別する。"""
    utc = datetime(2026, 9, 13, 1, tzinfo=UTC)
    jst = datetime.fromisoformat("2026-09-13T10:00:00+09:00")
    build = build_acquisition_request_id
    original = build(FetchCadence.HIGH, utc, 123)
    assert build(FetchCadence.HIGH, jst, 123) == original
    assert (
        len(
            {
                original,
                build(FetchCadence.MEDIUM, utc, 123),
                build(FetchCadence.HIGH, utc + timedelta(minutes=15), 123),
                build(FetchCadence.HIGH, utc, 124),
            }
        )
        == 4
    )


@pytest.mark.parametrize(
    "scheduled_at",
    [
        "2026-09-13T01:00:00",
        "2026-09-13T01:00:00.001Z",
        "2026-09-13T01:00:00.0000001Z",
        datetime(2026, 9, 13, 1),
        datetime(2026, 9, 13, 1, microsecond=1, tzinfo=UTC),
    ],
)
def test_schedule_rejects_time_that_would_require_completion_or_rounding(scheduled_at):
    """予定時刻を補完・丸めなければ受け取れない入力を拒否する。"""
    with pytest.raises(ValidationError):
        SourceDispatchInput.model_validate(
            {"cadence": "high", "scheduled_at": scheduled_at}
        )
