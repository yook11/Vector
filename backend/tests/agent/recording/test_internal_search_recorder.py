"""LogfireInternalSearchRecorderの契約。"""

from __future__ import annotations

import asyncio
from types import TracebackType

import pytest
from logfire.testing import CaptureLogfire

from app.agent.evidence_collection.internal_search.contract import (
    InternalSearchError,
    InternalSearchFailureCode,
)
from app.agent.recording.internal_search import (
    InternalSearchFailed,
    InternalSearchSucceeded,
    LogfireInternalSearchRecorder,
)
from tests.logfire._metric_helpers import attributes_of, collected_metrics
from tests.logfire._span_helpers import domain_attr_keys, one_span_named

_OUTCOME_METRIC = "vector.agent.internal_retrieval.outcome"
_DURATION_METRIC = "vector.agent.internal_search.duration"
_SPAN_NAME = "internal_search"


async def test_success_emits_span_duration_and_existing_outcome(
    capfire: CaptureLogfire,
) -> None:
    async with LogfireInternalSearchRecorder().record(query_count=2) as recording:
        recording.report_outcome(InternalSearchSucceeded(hit_count=2))

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "succeeded",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "succeeded",
        "query_count": 2,
    }
    span = one_span_named(capfire, _SPAN_NAME)
    assert domain_attr_keys(span["attributes"]) == set()


async def test_empty_emits_completed_duration_and_existing_outcome(
    capfire: CaptureLogfire,
) -> None:
    async with LogfireInternalSearchRecorder().record(query_count=1) as recording:
        recording.report_outcome(InternalSearchSucceeded(hit_count=0))

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "empty",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "empty",
        "query_count": 1,
    }


async def test_classified_failure_emits_failed_duration_and_failure_code(
    capfire: CaptureLogfire,
) -> None:
    error = InternalSearchError(
        code=InternalSearchFailureCode.QUERY_EMBEDDING_FAILED
    )

    with pytest.raises(InternalSearchError) as raised:
        async with LogfireInternalSearchRecorder().record(query_count=1) as recording:
            recording.report_outcome(
                InternalSearchFailed(
                    failure_code=InternalSearchFailureCode.QUERY_EMBEDDING_FAILED
                )
            )
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "failed",
        "outcome": "failed",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "failed",
        "query_count": 1,
        "failure_code": "query_embedding_failed",
    }


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(asyncio.CancelledError(), id="cancelled"),
        pytest.param(GeneratorExit(), id="generator-exit"),
    ],
)
async def test_stop_emits_stopped_duration_without_outcome(
    error: BaseException,
    capfire: CaptureLogfire,
) -> None:
    with pytest.raises(type(error)) as raised:
        async with LogfireInternalSearchRecorder().record(query_count=1):
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "stopped",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_unclassified_error_emits_failed_duration_without_outcome(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("search bug")

    with pytest.raises(RuntimeError) as raised:
        async with LogfireInternalSearchRecorder().record(query_count=1):
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "failed",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_error_after_success_discards_outcome(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("failed after outcome")

    with pytest.raises(RuntimeError) as raised:
        async with LogfireInternalSearchRecorder().record(query_count=1) as recording:
            recording.report_outcome(InternalSearchSucceeded(hit_count=1))
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "failed",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_clock_failure_skips_duration_but_preserves_outcome(
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    monkeypatch.setattr(
        "app.agent.recording.internal_search.perf_counter",
        lambda: (_ for _ in ()).throw(RuntimeError("clock failed")),
    )

    async with LogfireInternalSearchRecorder().record(query_count=1) as recording:
        recording.report_outcome(InternalSearchSucceeded(hit_count=1))

    metrics = collected_metrics(capfire)
    assert all(item["name"] != _DURATION_METRIC for item in metrics)
    assert attributes_of(metrics, _OUTCOME_METRIC)["result"] == "succeeded"


async def test_duration_failure_does_not_block_outcome(
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("duration failed")

    monkeypatch.setattr(
        "app.agent.recording.internal_search._duration_histogram.record",
        _boom,
    )
    async with LogfireInternalSearchRecorder().record(query_count=1) as recording:
        recording.report_outcome(InternalSearchSucceeded(hit_count=1))

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC)["result"] == (
        "succeeded"
    )


async def test_outcome_failure_does_not_block_duration(
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("outcome failed")

    monkeypatch.setattr(
        "app.agent.evidence_collection.internal_search.metrics."
        "record_internal_retrieval_outcome",
        _boom,
    )
    async with LogfireInternalSearchRecorder().record(query_count=1) as recording:
        recording.report_outcome(InternalSearchSucceeded(hit_count=1))

    assert attributes_of(collected_metrics(capfire), _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "succeeded",
    }


@pytest.mark.parametrize("failure_point", ["create", "enter", "exit"])
async def test_span_failure_does_not_change_business_result(
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    class FailingSpan:
        def __enter__(self) -> None:
            if failure_point == "enter":
                raise RuntimeError("span enter failed")

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: TracebackType | None,
        ) -> bool:
            if failure_point == "exit":
                raise RuntimeError("span exit failed")
            return False

    def _span(*_args: object, **_kwargs: object) -> FailingSpan:
        if failure_point == "create":
            raise RuntimeError("span create failed")
        return FailingSpan()

    monkeypatch.setattr("app.agent.recording.internal_search.logfire.span", _span)
    async with LogfireInternalSearchRecorder().record(query_count=1) as recording:
        recording.report_outcome(InternalSearchSucceeded(hit_count=1))

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC)["result"] == (
        "succeeded"
    )


def test_negative_query_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="query_count"):
        LogfireInternalSearchRecorder().record(query_count=-1)


def test_negative_hit_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="hit_count"):
        InternalSearchSucceeded(hit_count=-1)
