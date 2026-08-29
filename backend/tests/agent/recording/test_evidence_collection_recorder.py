"""LogfireEvidenceCollectionRecorderのspan階層契約。"""

from __future__ import annotations

import asyncio
from types import TracebackType

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.evidence_collection import LogfireEvidenceCollectionRecorder
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
    spans_named,
)

_PHASE_SPAN_NAME = "agent_phase"
_TASK_SPAN_NAME = "evidence_collection_task"


async def test_collect_span_parents_each_task_span(capfire: CaptureLogfire) -> None:
    async with LogfireEvidenceCollectionRecorder().record() as recording:

        async def run_task(task_index: int) -> None:
            async with recording.record_task(task_index=task_index):
                await asyncio.sleep(0)

        await asyncio.gather(run_task(0), run_task(1))

    collection_span = one_span_named(capfire, _PHASE_SPAN_NAME)
    task_spans = spans_named(capfire, _TASK_SPAN_NAME)
    assert domain_attr_keys(collection_span["attributes"]) == {"phase"}
    assert collection_span["attributes"]["phase"] == "evidence_collection"
    assert len(task_spans) == 2
    assert {span["attributes"]["task_index"] for span in task_spans} == {0, 1}
    assert all(
        span["parent"]["span_id"] == collection_span["context"]["span_id"]
        for span in task_spans
    )


async def test_unclassified_error_marks_task_and_collection_and_preserves_identity(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("collection failed")

    with pytest.raises(RuntimeError) as raised:
        async with LogfireEvidenceCollectionRecorder().record() as recording:
            async with recording.record_task(task_index=0):
                raise error

    assert raised.value is error
    assert exception_event(one_span_named(capfire, _TASK_SPAN_NAME)) is not None
    assert exception_event(one_span_named(capfire, _PHASE_SPAN_NAME)) is not None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(asyncio.CancelledError(), id="cancelled"),
        pytest.param(GeneratorExit(), id="generator-exit"),
    ],
)
async def test_stop_closes_task_and_collection_without_error_event(
    error: BaseException,
    capfire: CaptureLogfire,
) -> None:
    with pytest.raises(type(error)) as raised:
        async with LogfireEvidenceCollectionRecorder().record() as recording:
            async with recording.record_task(task_index=0):
                raise error

    assert raised.value is error
    assert exception_event(one_span_named(capfire, _TASK_SPAN_NAME)) is None
    assert exception_event(one_span_named(capfire, _PHASE_SPAN_NAME)) is None


@pytest.mark.parametrize("failure_point", ["create", "enter", "exit"])
async def test_collection_span_failure_does_not_change_business_result(
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
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
        "app.agent.recording.evidence_collection.agent_phase",
        _phase,
    )
    async with LogfireEvidenceCollectionRecorder().record() as recording:
        async with recording.record_task(task_index=0):
            result = "collected"

    assert result == "collected"


@pytest.mark.parametrize("failure_point", ["create", "enter", "exit"])
async def test_task_span_failure_does_not_change_business_result(
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr(
        "app.agent.recording.evidence_collection.logfire.span",
        _span,
    )
    async with LogfireEvidenceCollectionRecorder().record() as recording:
        async with recording.record_task(task_index=0):
            result = "collected"

    assert result == "collected"


def test_negative_task_index_is_rejected() -> None:
    async def _record() -> None:
        async with LogfireEvidenceCollectionRecorder().record() as recording:
            recording.record_task(task_index=-1)

    with pytest.raises(ValueError, match="task_index"):
        asyncio.run(_record())
