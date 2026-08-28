"""LogfireEvidenceAnswerRecorder の契約。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import TracebackType

import pytest
from logfire.testing import CaptureLogfire

from app.agent.contract import AnswerGenerationStopped
from app.agent.recording.evidence_answer import (
    EvidenceAnswerFailed,
    EvidenceAnswerRecordingOutcome,
    EvidenceAnswerSucceeded,
    LogfireEvidenceAnswerRecorder,
)
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.evidence_answer.outcome"
_DURATION_METRIC = "vector.agent.evidence_answer.duration"


async def test_success_emits_completed_duration_and_outcome(
    capfire: CaptureLogfire,
) -> None:
    async with LogfireEvidenceAnswerRecorder().record(
        agent_name="evidence_answer"
    ) as recording:
        recording.set_outcome(EvidenceAnswerSucceeded(attempt_count=2))

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "succeeded",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "succeeded",
        "attempt_count": 2,
        "failure_code": "none",
    }


async def test_classified_failure_emits_completed_duration_and_failed_outcome(
    capfire: CaptureLogfire,
) -> None:
    async with LogfireEvidenceAnswerRecorder().record(
        agent_name="evidence_answer"
    ) as recording:
        recording.set_outcome(
            EvidenceAnswerFailed(
                failure_code="ai_error_network",
                attempt_count=1,
            )
        )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "failed",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "failed",
        "attempt_count": 1,
        "failure_code": "ai_error_network",
    }


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(asyncio.CancelledError(), id="cancellation"),
        pytest.param(GeneratorExit(), id="generator-exit"),
        pytest.param(AnswerGenerationStopped(), id="generation-stopped"),
    ],
)
async def test_stop_emits_stopped_duration_without_outcome(
    error: BaseException,
    capfire: CaptureLogfire,
) -> None:
    with pytest.raises(type(error)) as raised:
        async with LogfireEvidenceAnswerRecorder().record(agent_name="evidence_answer"):
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "stopped",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_stop_after_outcome_discards_outcome(
    capfire: CaptureLogfire,
) -> None:
    error = AnswerGenerationStopped()

    with pytest.raises(AnswerGenerationStopped) as raised:
        async with LogfireEvidenceAnswerRecorder().record(
            agent_name="evidence_answer"
        ) as recording:
            recording.set_outcome(EvidenceAnswerSucceeded(attempt_count=1))
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "stopped",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(EvidenceAnswerSucceeded(attempt_count=1), id="success"),
        pytest.param(
            EvidenceAnswerFailed(failure_code="known", attempt_count=1),
            id="classified-failure",
        ),
    ],
)
async def test_error_after_outcome_discards_outcome(
    outcome: EvidenceAnswerRecordingOutcome,
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("failed after outcome was selected")

    with pytest.raises(RuntimeError) as raised:
        async with LogfireEvidenceAnswerRecorder().record(
            agent_name="evidence_answer"
        ) as recording:
            recording.set_outcome(outcome)
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "failed",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_missing_outcome_emits_failed_duration_only(
    capfire: CaptureLogfire,
) -> None:
    async with LogfireEvidenceAnswerRecorder().record(agent_name="evidence_answer"):
        pass

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

    monkeypatch.setattr(
        "app.agent.recording.evidence_answer.perf_counter",
        _boom,
    )

    async with LogfireEvidenceAnswerRecorder().record(
        agent_name="evidence_answer"
    ) as recording:
        recording.set_outcome(EvidenceAnswerSucceeded(attempt_count=1))

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
        "app.agent.recording.evidence_answer._duration_histogram.record",
        _boom,
    )
    async with LogfireEvidenceAnswerRecorder().record(
        agent_name="evidence_answer"
    ) as recording:
        recording.set_outcome(EvidenceAnswerSucceeded(attempt_count=1))

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
        "app.agent.answering.metrics.record_evidence_answer_outcome",
        _boom,
    )
    async with LogfireEvidenceAnswerRecorder().record(
        agent_name="evidence_answer"
    ) as recording:
        recording.set_outcome(EvidenceAnswerSucceeded(attempt_count=1))

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

    def _phase(**_kwargs: object) -> FailingSpan:
        if failure_point == "create":
            raise RuntimeError("span creation failed")
        return FailingSpan()

    monkeypatch.setattr("app.agent.recording.evidence_answer.agent_phase", _phase)
    async with LogfireEvidenceAnswerRecorder().record(
        agent_name="evidence_answer"
    ) as recording:
        recording.set_outcome(EvidenceAnswerSucceeded(attempt_count=1))

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC)["result"] == (
        "succeeded"
    )


async def test_span_exit_failure_does_not_replace_business_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_error = RuntimeError("business failed")

    class FailingSpan:
        def __enter__(self) -> None:
            return None

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: TracebackType | None,
        ) -> bool:
            raise RuntimeError("span exit failed")

    monkeypatch.setattr(
        "app.agent.recording.evidence_answer.agent_phase",
        lambda **_kwargs: FailingSpan(),
    )

    with pytest.raises(RuntimeError) as raised:
        async with LogfireEvidenceAnswerRecorder().record(agent_name="evidence_answer"):
            raise business_error

    assert raised.value is business_error


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(
            lambda: EvidenceAnswerSucceeded(attempt_count=0),
            id="success-attempt-count",
        ),
        pytest.param(
            lambda: EvidenceAnswerFailed(failure_code="", attempt_count=1),
            id="failure-code",
        ),
        pytest.param(
            lambda: EvidenceAnswerFailed(failure_code="known", attempt_count=0),
            id="failure-attempt-count",
        ),
    ],
)
def test_outcome_rejects_invalid_recording_values(
    outcome: Callable[[], EvidenceAnswerRecordingOutcome],
) -> None:
    with pytest.raises(ValueError):
        outcome()
