"""LogfireExternalSearchRecorderの契約。"""

from __future__ import annotations

import asyncio
from types import TracebackType

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.external_search import (
    ExternalSearchProviderFailed,
    ExternalSearchQueryGenerationFailed,
    ExternalSearchSucceeded,
    LogfireExternalSearchRecorder,
)
from tests.logfire._metric_helpers import attributes_of, collected_metrics
from tests.logfire._span_helpers import domain_attr_keys, one_span_named

_OUTCOME_METRIC = "vector.agent.external_search.outcome"
_DURATION_METRIC = "vector.agent.external_search.duration"
_SEARCH_SPAN_NAME = "external_search"
_PHASE_SPAN_NAME = "agent_phase"


async def test_success_emits_search_and_query_spans_duration_and_outcome(
    capfire: CaptureLogfire,
) -> None:
    async with LogfireExternalSearchRecorder().record() as recording:
        async with recording.record_query_generation(
            agent_name="external_query_generator"
        ):
            pass
        recording.report_outcome(ExternalSearchSucceeded())

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "succeeded",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {"result": "succeeded"}
    search_span = one_span_named(capfire, _SEARCH_SPAN_NAME)
    query_span = one_span_named(capfire, _PHASE_SPAN_NAME)
    assert domain_attr_keys(search_span["attributes"]) == set()
    assert domain_attr_keys(query_span["attributes"]) == {
        "phase",
        "agent_name",
    }
    assert query_span["parent"]["span_id"] == search_span["context"]["span_id"]


@pytest.mark.parametrize(
    ("outcome", "label"),
    [
        pytest.param(
            ExternalSearchQueryGenerationFailed(),
            "query_generation_failed",
            id="query-generation",
        ),
        pytest.param(
            ExternalSearchProviderFailed(),
            "provider_failed",
            id="provider",
        ),
    ],
)
async def test_classified_failure_is_completed_with_failed_conclusion(
    outcome: ExternalSearchQueryGenerationFailed | ExternalSearchProviderFailed,
    label: str,
    capfire: CaptureLogfire,
) -> None:
    async with LogfireExternalSearchRecorder().record() as recording:
        recording.report_outcome(outcome)

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": label,
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {"result": label}


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
        async with LogfireExternalSearchRecorder().record():
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
        async with LogfireExternalSearchRecorder().record():
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
        async with LogfireExternalSearchRecorder().record() as recording:
            recording.report_outcome(ExternalSearchSucceeded())
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
    def _boom() -> float:
        raise RuntimeError("clock failed")

    monkeypatch.setattr("app.agent.recording.external_search.perf_counter", _boom)
    async with LogfireExternalSearchRecorder().record() as recording:
        recording.report_outcome(ExternalSearchSucceeded())

    metrics = collected_metrics(capfire)
    assert all(item["name"] != _DURATION_METRIC for item in metrics)
    assert attributes_of(metrics, _OUTCOME_METRIC) == {"result": "succeeded"}


async def test_duration_failure_does_not_block_outcome(
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("duration failed")

    monkeypatch.setattr(
        "app.agent.recording.external_search._duration_histogram.record",
        _boom,
    )
    async with LogfireExternalSearchRecorder().record() as recording:
        recording.report_outcome(ExternalSearchSucceeded())

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC) == {
        "result": "succeeded"
    }


async def test_outcome_failure_does_not_block_duration(
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("outcome failed")

    monkeypatch.setattr(
        "app.agent.recording.external_search._outcome_counter.add",
        _boom,
    )
    async with LogfireExternalSearchRecorder().record() as recording:
        recording.report_outcome(ExternalSearchSucceeded())

    assert attributes_of(collected_metrics(capfire), _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "succeeded",
    }


@pytest.mark.parametrize("failure_point", ["create", "enter", "exit"])
async def test_search_span_failure_does_not_change_business_result(
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

    monkeypatch.setattr("app.agent.recording.external_search.logfire.span", _span)
    async with LogfireExternalSearchRecorder().record() as recording:
        recording.report_outcome(ExternalSearchSucceeded())

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC) == {
        "result": "succeeded"
    }


@pytest.mark.parametrize("failure_point", ["create", "enter", "exit"])
async def test_query_span_failure_does_not_change_business_result(
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

    def _phase(**_kwargs: object) -> FailingSpan:
        if failure_point == "create":
            raise RuntimeError("span create failed")
        return FailingSpan()

    monkeypatch.setattr(
        "app.agent.recording.external_search.agent_phase",
        _phase,
    )

    async with LogfireExternalSearchRecorder().record() as recording:
        async with recording.record_query_generation(agent_name="query_agent"):
            pass
        recording.report_outcome(ExternalSearchSucceeded())

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC) == {
        "result": "succeeded"
    }
