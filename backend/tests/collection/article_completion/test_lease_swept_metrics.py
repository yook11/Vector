"""completion lease sweep の Logfire metric 契約。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from app.collection.article_completion import metrics as completion_metrics
from app.queue.tasks.completion import sweep_expired_leases
from tests.logfire._metric_helpers import collected_metrics

_METRIC = "vector.completion.lease_swept"
_RECORDER = "record_completion_lease_swept"


def _find_metric(metrics: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((metric for metric in metrics if metric["name"] == _METRIC), None)


def _sum_value(metric: dict[str, Any]) -> int:
    return sum(int(point["value"]) for point in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [point.get("attributes", {}) for point in metric["data"]["data_points"]]


def _recorder() -> Callable[[int], None]:
    """missing helperをcollection errorでなく明示的なcontract failureにする。"""
    recorder = getattr(completion_metrics, _RECORDER, None)
    assert callable(recorder), (
        "completion metrics must publish record_completion_lease_swept(swept_count)"
    )
    return cast(Callable[[int], None], recorder)


class _AsyncSessionStub:
    def __init__(self) -> None:
        self.commit = AsyncMock()

    async def __aenter__(self) -> _AsyncSessionStub:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _ctx(session_factory: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    return ctx


async def _invoke_sweep_task(swept_count: int) -> tuple[dict[str, int], AsyncMock]:
    session = _AsyncSessionStub()
    session_factory = MagicMock(return_value=session)
    repository = MagicMock()
    repository.sweep_expired_leases = AsyncMock(return_value=swept_count)

    with patch(
        "app.queue.tasks.completion.ArticleCompletionRepository",
        return_value=repository,
    ):
        result = await sweep_expired_leases(ctx=_ctx(session_factory))

    session.commit.assert_awaited_once_with()
    return result, repository.sweep_expired_leases


def test_record_completion_lease_swept_adds_count_without_attributes(
    capfire: CaptureLogfire,
) -> None:
    """正の swept 件数は unit=1 の attribute なし counter にそのまま加算する。"""
    _recorder()(3)

    metric = _find_metric(collected_metrics(capfire))
    assert metric is not None
    assert (metric["unit"], _sum_value(metric), _attributes_for(metric)) == (
        "1",
        3,
        [{}],
    )


def test_record_completion_lease_swept_does_not_emit_zero(
    capfire: CaptureLogfire,
) -> None:
    """count=0 は counter data point 自体を emit しない。"""
    _recorder()(0)

    assert _find_metric(collected_metrics(capfire)) is None


@pytest.mark.asyncio
async def test_sweep_task_records_repository_count_and_preserves_result(
    capfire: CaptureLogfire,
) -> None:
    """sweep task は repository の正の件数を metric 境界へ渡して同じ値を返す。"""
    result, sweep = await _invoke_sweep_task(4)

    metric = _find_metric(collected_metrics(capfire))
    assert metric is not None
    assert (
        result,
        sweep.await_count,
        _sum_value(metric),
        _attributes_for(metric),
    ) == ({"swept_count": 4}, 1, 4, [{}])


@pytest.mark.asyncio
async def test_sweep_task_does_not_emit_metric_for_zero(
    capfire: CaptureLogfire,
) -> None:
    """repository が 0 を返す tick は戻り値を保ち metric を emit しない。"""
    result, sweep = await _invoke_sweep_task(0)

    assert (
        result,
        sweep.await_count,
        _find_metric(collected_metrics(capfire)),
    ) == ({"swept_count": 0}, 1, None)
