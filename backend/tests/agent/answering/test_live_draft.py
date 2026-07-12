"""1 generation 分の回答下書きライブ表示セッション契約。"""

from __future__ import annotations

import asyncio

import pytest

from app.agent.answering.live_delivery import BestEffortAnswerDeltaReporter
from app.agent.answering.live_draft import LiveAnswerDraftSession
from app.agent.contract import AnswerDeltaReporter


class RecordingDeltaReporter:
    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self.fail_on = fail_on
        self.operations: list[tuple[str, int, str | None]] = []

    async def append(self, *, generation: int, text: str) -> None:
        self.operations.append(("append", generation, text))
        if "append" in self.fail_on:
            raise RuntimeError("reporter append unavailable")

    async def reset(self, *, generation: int) -> None:
        self.operations.append(("reset", generation, None))
        if "reset" in self.fail_on:
            raise RuntimeError("reporter reset unavailable")

    async def finish(self, *, generation: int) -> None:
        self.operations.append(("finish", generation, None))
        if "finish" in self.fail_on:
            raise RuntimeError("reporter finish unavailable")

    async def abort(self, *, generation: int) -> None:
        self.operations.append(("abort", generation, None))
        if "abort" in self.fail_on:
            raise RuntimeError("reporter abort unavailable")


def _session(reporter: AnswerDeltaReporter) -> LiveAnswerDraftSession:
    return LiveAnswerDraftSession(generation=7, delta_reporter=reporter)


@pytest.mark.asyncio
async def test_append_reports_only_incrementally_visible_answer_text() -> None:
    reporter = RecordingDeltaReporter()

    async with _session(reporter) as session:
        await session.append(" \t前[[")
        await session.append("1]] 後 \n")
        await session.commit()

    assert reporter.operations == [
        ("append", 7, "前"),
        ("append", 7, " 後"),
        ("finish", 7, None),
    ]


@pytest.mark.asyncio
async def test_commit_appends_filter_tail_before_finish() -> None:
    reporter = RecordingDeltaReporter()

    async with _session(reporter) as session:
        await session.append("本文 [")
        await session.commit()

    assert reporter.operations == [
        ("append", 7, "本文"),
        ("append", 7, " ["),
        ("finish", 7, None),
    ]


@pytest.mark.asyncio
async def test_abort_discards_filter_tail_and_does_not_finish() -> None:
    reporter = RecordingDeltaReporter()

    async with _session(reporter) as session:
        await session.append("本文 [")
        await session.abort()

    assert reporter.operations == [
        ("append", 7, "本文"),
        ("abort", 7, None),
    ]


@pytest.mark.asyncio
async def test_normal_context_exit_without_commit_aborts() -> None:
    reporter = RecordingDeltaReporter()

    async with _session(reporter) as session:
        await session.append("本文")

    assert reporter.operations == [
        ("append", 7, "本文"),
        ("abort", 7, None),
    ]


@pytest.mark.asyncio
async def test_exception_context_exit_aborts_without_suppressing_exception() -> None:
    reporter = RecordingDeltaReporter()
    expected = RuntimeError("generation failed")

    with pytest.raises(RuntimeError) as exc_info:
        async with _session(reporter) as session:
            await session.append("本文")
            raise expected

    assert exc_info.value is expected
    assert reporter.operations == [
        ("append", 7, "本文"),
        ("abort", 7, None),
    ]


@pytest.mark.asyncio
async def test_cancelled_context_exit_aborts_without_suppressing_cancellation() -> None:
    reporter = RecordingDeltaReporter()
    expected = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        async with _session(reporter) as session:
            await session.append("本文")
            raise expected

    assert exc_info.value is expected
    assert reporter.operations == [
        ("append", 7, "本文"),
        ("abort", 7, None),
    ]


@pytest.mark.parametrize("failing_method", ["append", "finish", "abort"])
@pytest.mark.asyncio
async def test_reporter_failure_is_best_effort(failing_method: str) -> None:
    reporter = RecordingDeltaReporter(fail_on=frozenset({failing_method}))
    best_effort_reporter = BestEffortAnswerDeltaReporter(reporter)

    async with _session(best_effort_reporter) as session:
        await session.append("本文")
        if failing_method != "abort":
            await session.commit()

    expected_close = "abort" if failing_method == "abort" else "finish"
    assert reporter.operations == [
        ("append", 7, "本文"),
        (expected_close, 7, None),
    ]


@pytest.mark.asyncio
async def test_abort_is_idempotent() -> None:
    reporter = RecordingDeltaReporter()

    async with _session(reporter) as session:
        await session.abort()
        await session.abort()

    assert reporter.operations == [("abort", 7, None)]


@pytest.mark.parametrize("close_method", ["commit", "abort"])
@pytest.mark.parametrize("closed_operation", ["append", "commit"])
@pytest.mark.asyncio
async def test_append_and_commit_raise_after_session_is_closed(
    close_method: str,
    closed_operation: str,
) -> None:
    reporter = RecordingDeltaReporter()

    async with _session(reporter) as session:
        await getattr(session, close_method)()

        with pytest.raises(RuntimeError, match="closed"):
            if closed_operation == "append":
                await session.append("遅い断片")
            else:
                await session.commit()
