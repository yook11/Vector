"""Direct answer flow tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

import app.agent.answering.direct_answer.contract as direct_answer_contract
import app.agent.contract as shared_agent_contract
from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.direct_answer.contract import (
    AnswerGenerationStopped,
    DirectAnswerDraft,
    DirectAnswerInput,
    DirectAnswerInvalidError,
)
from app.agent.answering.direct_answer.flow import DirectAnswerFlow
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderNetworkError,
    AIProviderOutputTruncatedError,
)
from app.analysis.gemini_error_translator import GeminiStateReason
from tests.agent.recording._fakes import RecordingDirectAnswerRecorder
from tests.logfire._metric_helpers import collected_metrics

_DIRECT_ANSWER_OUTCOME_METRIC = "vector.agent.direct_answer.outcome"


def _metric_attributes(
    metrics: list[dict[str, Any]],
    metric_name: str,
) -> list[dict[str, Any]]:
    metric = next((item for item in metrics if item["name"] == metric_name), None)
    if metric is None:
        return []
    return [
        data_point.get("attributes", {}) for data_point in metric["data"]["data_points"]
    ]


def _as_of() -> datetime:
    return datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def _truncated_error() -> AIProviderOutputTruncatedError:
    """S1 runtimeが実際に送出する形 (reason付き) を再現する。"""
    return AIProviderOutputTruncatedError(
        reason=GeminiStateReason.OUTPUT_TOKEN_LIMIT_REACHED
    )


def _request() -> AnsweringRequest:
    return AnsweringRequest(
        question="Vector の使い方を短く教えて",
        history=(
            ThreadMessageSnapshot(
                role="assistant",
                content="前回は基本操作を説明済み",
            ),
        ),
        as_of=_as_of(),
    )


StreamOutcome = str | Sequence[str] | BaseException


class FakeDirectAnswerStream:
    def __init__(self, outcome: StreamOutcome) -> None:
        if isinstance(outcome, BaseException):
            self._items: list[str | BaseException] = [outcome]
        elif isinstance(outcome, str):
            self._items = [outcome]
        else:
            self._items = list(outcome)
        self.closed = False

    def __aiter__(self) -> FakeDirectAnswerStream:
        return self

    async def __anext__(self) -> str:
        if not self._items:
            raise StopAsyncIteration
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


class FakeDirectAnswerGenerator:
    def __init__(self, outcomes: Sequence[StreamOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeDirectAnswerStream] = []
        self.activations = 0
        self.exits = 0

    def stream_text(
        self,
        agent: object,
        input: DirectAnswerInput,
        *,
        attempt_number: int,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "agent": agent,
                "request": input.request,
                "previous_answer": input.previous_answer,
                "repair_context": input.repair_context,
                "previous_output_truncated": input.previous_output_truncated,
                "attempt_number": attempt_number,
            }
        )
        outcome = self._outcomes.pop(0)
        stream = FakeDirectAnswerStream(outcome)
        self.streams.append(stream)
        return stream

    @asynccontextmanager
    async def activate(self) -> AsyncIterator[FakeDirectAnswerGenerator]:
        self.activations += 1
        try:
            yield self
        finally:
            self.exits += 1


class RecordingDeltaReporter:
    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self.fail_on = fail_on
        self.appended: list[tuple[int, str]] = []
        self.finished: list[int] = []
        self.aborted: list[int] = []
        self.reset_calls = 0

    async def append(self, *, generation: int, text: str) -> None:
        self.appended.append((generation, text))
        if "append" in self.fail_on:
            raise RuntimeError("reporter append unavailable")

    async def finish(self, *, generation: int) -> None:
        self.finished.append(generation)
        if "finish" in self.fail_on:
            raise RuntimeError("reporter finish unavailable")

    async def abort(self, *, generation: int) -> None:
        self.aborted.append(generation)
        if "abort" in self.fail_on:
            raise RuntimeError("reporter abort unavailable")

    async def reset(self, *, generation: int) -> None:
        self.reset_calls += 1


class SequenceContinuation:
    def __init__(self, results: Sequence[bool]) -> None:
        self._results = list(results)
        self.calls = 0

    async def should_continue(self) -> bool:
        self.calls += 1
        if not self._results:
            return True
        return self._results.pop(0)


def test_answer_generation_stopped_is_shared_identity_compatible_reexport() -> None:
    assert (
        direct_answer_contract.AnswerGenerationStopped
        is shared_agent_contract.AnswerGenerationStopped
    )


async def _answer(
    generator: FakeDirectAnswerGenerator,
    *,
    delta_reporter: RecordingDeltaReporter | None = None,
    continuation: SequenceContinuation | None = None,
    recorder: RecordingDirectAnswerRecorder | None = None,
) -> DirectAnswerDraft:
    if recorder is None:
        flow = DirectAnswerFlow(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=generator.activate,
            delta_reporter=delta_reporter,
            continuation=continuation,
        )
    else:
        flow = DirectAnswerFlow(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=generator.activate,
            delta_reporter=delta_reporter,
            continuation=continuation,
            recorder=recorder,
        )
    return await flow.answer(
        request=_request(),
        previous_answer="",
    )


def _assert_recorded(
    recorder: RecordingDirectAnswerRecorder,
    *,
    outcome: str | None,
    stopped: bool = False,
    retry_used: bool = False,
    failure_code: str | None = None,
) -> None:
    assert len(recorder.starts) == 1
    assert len(recorder.ends) == 1
    recorded = recorder.ends[0]
    assert recorded.call is recorder.starts[0]
    assert recorded.outcome == outcome
    assert recorded.stopped is stopped
    assert recorded.retry_used is retry_used
    assert recorded.failure_code == failure_code


@pytest.mark.asyncio
async def test_valid_text_returns_direct_draft_without_retry(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeDirectAnswerGenerator(["検索なしで回答できます。"])
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft == DirectAnswerDraft(answer="検索なしで回答できます。")
    assert len(generator.calls) == 1
    assert generator.calls[0]["repair_context"] is None
    assert reporter.finished == [1]
    assert reporter.aborted == []
    assert generator.streams[0].closed is True

    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "answered",
            "retry_used": False,
            "failure_code": "none",
        }
    ]


@pytest.mark.asyncio
async def test_direct_answer_removes_inline_citation_markers_after_generation() -> None:
    generator = FakeDirectAnswerGenerator(
        ["結論は維持します。[[1]] 詳細は省略します。[[2]]"]
    )

    draft = await DirectAnswerFlow(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=generator.activate,
    ).answer(
        request=AnsweringRequest(
            question="前回の結論だけ",
            history=(
                ThreadMessageSnapshot(role="assistant", content="根拠は説明済み"),
            ),
            as_of=_as_of(),
        ),
        previous_answer="根拠付き前回答 [[1]]",
    )

    assert draft.answer == "結論は維持します。 詳細は省略します。"
    assert generator.calls[0]["request"].question == "前回の結論だけ"
    assert generator.calls[0]["request"].history == (
        ThreadMessageSnapshot(role="assistant", content="根拠は説明済み"),
    )
    assert generator.calls[0]["previous_answer"] == "根拠付き前回答 [[1]]"


# --- グループ形 marker 受理 (spec: agent-citation-marker-grouped-refs-slice.md) ---
# direct answerの除去経路も正準形と同様にグループ形を除去対象とする。


@pytest.mark.asyncio
async def test_direct_answer_removes_group_form_citation_markers_after_generation() -> (
    None
):
    """グループ形 [[1], [2]] も正準形と同様に本文から除去される。"""
    generator = FakeDirectAnswerGenerator(
        ["結論は維持します。[[1], [2]] 詳細は省略します。"]
    )

    draft = await _answer(generator)

    assert draft.answer == "結論は維持します。 詳細は省略します。"


@pytest.mark.asyncio
async def test_group_form_marker_only_generation_retries_then_raises_invalid() -> None:
    """本文がグループ形markerだけで構成されると除去後は空扱いとなり、
    通常の空回答と同じretry -> invalid経路をたどってDirectAnswerInvalidErrorになる。"""
    generator = FakeDirectAnswerGenerator(["[[1], [2]]", "[[1], [3]]"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerInvalidError):
        await _answer(generator, delta_reporter=reporter)

    assert len(generator.calls) == 2
    assert reporter.aborted == [1, 2]


@pytest.mark.asyncio
async def test_blank_then_valid_retries_once_with_repair_context(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeDirectAnswerGenerator([" \n\t", "再試行後の回答です。"])

    draft = await _answer(generator)

    assert draft.answer == "再試行後の回答です。"
    assert [call["repair_context"] for call in generator.calls] == [
        None,
        "direct_answer_blank_response",
    ]
    assert [call["attempt_number"] for call in generator.calls] == [1, 2]
    assert all(call["agent"] is DIRECT_ANSWER_AGENT for call in generator.calls)
    assert generator.activations == 1
    assert generator.exits == 1
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "answered",
            "retry_used": True,
            "failure_code": "none",
        }
    ]


@pytest.mark.asyncio
async def test_blank_twice_raises_invalid_after_observation(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeDirectAnswerGenerator(["", " \n"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerInvalidError):
        await _answer(generator, delta_reporter=reporter)

    assert len(generator.calls) == 2
    assert reporter.appended == []
    assert reporter.finished == []
    assert reporter.aborted == [1, 2]
    assert all(stream.closed for stream in generator.streams)

    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "retry_used": True,
            "failure_code": "direct_answer_blank_response",
        }
    ]


@pytest.mark.asyncio
async def test_ai_provider_error_propagates_unwrapped_without_retry(
    capfire: CaptureLogfire,
) -> None:
    provider_exc = AIProviderNetworkError()
    generator = FakeDirectAnswerGenerator([provider_exc])
    reporter = RecordingDeltaReporter()

    with pytest.raises(AIProviderNetworkError) as exc_info:
        await _answer(generator, delta_reporter=reporter)

    assert exc_info.value is provider_exc
    assert len(generator.calls) == 1
    assert reporter.aborted == [1]
    assert generator.streams[0].closed is True

    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "retry_used": False,
            "failure_code": "ai_error_network",
        }
    ]


@pytest.mark.asyncio
async def test_second_truncation_raises_classified_truncation_error_after_retry(
    capfire: CaptureLogfire,
) -> None:
    """1回目のMAX_TOKENSはrequest内でretryされ、2回目も打ち切られたら分類済み
    errorが呼び出し元へ送出される。"""
    generator = FakeDirectAnswerGenerator([_truncated_error(), _truncated_error()])
    reporter = RecordingDeltaReporter()

    with pytest.raises(AIProviderOutputTruncatedError):
        await _answer(generator, delta_reporter=reporter)

    assert len(generator.calls) == 2
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "retry_used": True,
            "failure_code": "ai_error_output_truncated",
        }
    ]


@pytest.mark.asyncio
async def test_truncated_first_attempt_retries_with_truncation_flag() -> None:
    """R2条件6(flow層): 打ち切りはrequest内でretryされ、2回目の入力に

    previous_output_truncated=Trueが立つ (Evidence側S2と同じ受け渡し)。
    """
    generator = FakeDirectAnswerGenerator([_truncated_error(), "再試行後の回答です。"])

    draft = await _answer(generator)

    assert len(generator.calls) == 2
    assert generator.calls[0]["previous_output_truncated"] is False
    assert generator.calls[1]["previous_output_truncated"] is True
    assert draft.answer == "再試行後の回答です。"


@pytest.mark.asyncio
async def test_blank_retry_does_not_carry_truncation_flag() -> None:
    """R2条件7(flow層): 打ち切り以外(空回答)が原因のretryでは

    previous_output_truncatedを立てない。
    """
    generator = FakeDirectAnswerGenerator([" \n\t", "再試行後の回答です。"])

    draft = await _answer(generator)

    assert len(generator.calls) == 2
    assert generator.calls[0]["previous_output_truncated"] is False
    assert generator.calls[1]["previous_output_truncated"] is False
    assert draft.answer == "再試行後の回答です。"


@pytest.mark.asyncio
async def test_unexpected_exception_propagates_without_observation(
    capfire: CaptureLogfire,
) -> None:
    unexpected = RuntimeError("boom")
    generator = FakeDirectAnswerGenerator([unexpected])
    reporter = RecordingDeltaReporter()

    with pytest.raises(RuntimeError) as exc_info:
        await _answer(generator, delta_reporter=reporter)

    assert exc_info.value is unexpected
    assert len(generator.calls) == 1
    assert reporter.aborted == [1]
    assert generator.streams[0].closed is True
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == []


@pytest.mark.asyncio
async def test_runtime_scope_activation_failure_precedes_attempt_and_observation(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("runtime scope activation failed")
    reporter = RecordingDeltaReporter()

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[Any]:
        raise error
        yield  # pragma: no cover

    with pytest.raises(RuntimeError) as exc_info:
        await DirectAnswerFlow(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=broken_scope,
            delta_reporter=reporter,
        ).answer(request=_request())

    assert exc_info.value is error
    assert reporter.appended == []
    assert reporter.finished == []
    assert reporter.aborted == []
    assert (
        _metric_attributes(
            collected_metrics(capfire),
            _DIRECT_ANSWER_OUTCOME_METRIC,
        )
        == []
    )


@pytest.mark.asyncio
async def test_incremental_fragments_reconstruct_existing_final_answer() -> None:
    generator = FakeDirectAnswerGenerator(
        [[" \t回答", "[[1", "]] ", "の続き", "です。\n"]]
    )
    reporter = RecordingDeltaReporter()

    draft = await _answer(
        generator,
        delta_reporter=reporter,
    )

    assert draft == DirectAnswerDraft(answer="回答 の続きです。")
    assert "".join(text for _, text in reporter.appended) == draft.answer
    assert {generation for generation, _ in reporter.appended} == {1}
    assert reporter.finished == [1]
    assert reporter.aborted == []


@pytest.mark.asyncio
async def test_marker_only_blank_generation_retries_without_visible_reset() -> None:
    generator = FakeDirectAnswerGenerator(
        [
            ["[[", "1]]", " \n\u2003"],
            [" 再", "試行[[2]] ", "回答 "],
        ]
    )
    reporter = RecordingDeltaReporter()

    draft = await _answer(
        generator,
        delta_reporter=reporter,
    )

    assert draft == DirectAnswerDraft(answer="再試行 回答")
    generation_two_text = "".join(
        text for generation, text in reporter.appended if generation == 2
    )
    assert generation_two_text == draft.answer
    assert all(generation == 2 for generation, _ in reporter.appended)
    assert reporter.aborted == [1]
    assert reporter.finished == [2]
    assert reporter.reset_calls == 0
    assert all(stream.closed for stream in generator.streams)


@pytest.mark.parametrize("failing_method", ["append", "finish"])
@pytest.mark.asyncio
async def test_reporter_failure_does_not_change_success(
    failing_method: str,
) -> None:
    generator = FakeDirectAnswerGenerator([["回答", "です。"]])
    reporter = RecordingDeltaReporter(fail_on=frozenset({failing_method}))

    draft = await _answer(
        generator,
        delta_reporter=reporter,
    )

    assert draft == DirectAnswerDraft(answer="回答です。")


@pytest.mark.asyncio
async def test_reporter_abort_failure_does_not_mask_provider_error() -> None:
    provider_exc = AIProviderNetworkError()
    generator = FakeDirectAnswerGenerator([provider_exc])
    reporter = RecordingDeltaReporter(fail_on=frozenset({"abort"}))

    with pytest.raises(AIProviderNetworkError) as exc_info:
        await _answer(
            generator,
            delta_reporter=reporter,
        )

    assert exc_info.value is provider_exc
    assert reporter.aborted == [1]


@pytest.mark.asyncio
async def test_continuation_false_before_provider_start_is_routine_stop() -> None:
    assert not issubclass(AnswerGenerationStopped, AIProviderError)
    assert not issubclass(AnswerGenerationStopped, DirectAnswerInvalidError)
    generator = FakeDirectAnswerGenerator(["呼ばれない"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(AnswerGenerationStopped):
        await _answer(
            generator,
            delta_reporter=reporter,
            continuation=SequenceContinuation([False]),
        )

    assert generator.calls == []
    assert generator.streams == []
    assert reporter.appended == []
    assert reporter.aborted == [1]
    assert reporter.finished == []


@pytest.mark.asyncio
async def test_continuation_false_mid_stream_aborts_iterator_and_pending_report() -> (
    None
):
    generator = FakeDirectAnswerGenerator([["表示済み", "見せない本文"]])
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation([True, True, False])

    with pytest.raises(AnswerGenerationStopped):
        await _answer(
            generator,
            delta_reporter=reporter,
            continuation=continuation,
        )

    assert continuation.calls == 3
    assert "".join(text for _, text in reporter.appended) == "表示済み"
    assert reporter.aborted == [1]
    assert reporter.finished == []
    assert generator.streams[0].closed is True


@pytest.mark.asyncio
async def test_continuation_false_at_normal_stream_end_aborts_before_finish(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeDirectAnswerGenerator([["表示済み本文"]])
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation([True, True, False])

    with pytest.raises(AnswerGenerationStopped):
        await _answer(
            generator,
            delta_reporter=reporter,
            continuation=continuation,
        )

    assert continuation.calls == 3
    assert reporter.appended == [(1, "表示済み本文")]
    assert reporter.aborted == [1]
    assert reporter.finished == []
    assert generator.streams[0].closed is True
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == []


async def test_successful_answer_records_answered_outcome() -> None:
    """完成 draft は recorder へ answered を1回渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    generator = FakeDirectAnswerGenerator(["検索なしで回答できます。"])

    await _answer(generator, recorder=recorder)

    _assert_recorded(recorder, outcome="answered")


async def test_classified_failure_records_failed_outcome() -> None:
    """分類済み失敗は recorder へ failed を1回渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    generator = FakeDirectAnswerGenerator([AIProviderNetworkError()])

    with pytest.raises(AIProviderNetworkError):
        await _answer(generator, recorder=recorder)

    _assert_recorded(
        recorder,
        outcome="failed",
        failure_code="ai_error_network",
    )


async def test_retry_then_success_records_retry_used() -> None:
    """1回 retry した成功は retry_used=True で answered を渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    generator = FakeDirectAnswerGenerator([" \n\t", "再試行後の回答です。"])

    await _answer(generator, recorder=recorder)

    _assert_recorded(recorder, outcome="answered", retry_used=True)


async def test_generation_stop_records_stopped_without_outcome() -> None:
    """AnswerGenerationStopped は stopped とし、既存結論を渡さない。"""

    recorder = RecordingDirectAnswerRecorder()
    generator = FakeDirectAnswerGenerator(["呼ばれない"])

    with pytest.raises(AnswerGenerationStopped):
        await _answer(
            generator,
            continuation=SequenceContinuation([False]),
            recorder=recorder,
        )

    _assert_recorded(recorder, outcome=None, stopped=True)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("boom"), id="unknown"),
        pytest.param(asyncio.CancelledError(), id="cancellation"),
    ],
)
async def test_unclassified_failure_and_cancellation_record_without_outcome(
    error: BaseException,
) -> None:
    """未分類と cancel は既存結論を渡さず、cancel だけ stopped にする。"""

    recorder = RecordingDirectAnswerRecorder()
    generator = FakeDirectAnswerGenerator([error])

    with pytest.raises(type(error)):
        await _answer(generator, recorder=recorder)

    _assert_recorded(
        recorder,
        outcome=None,
        stopped=isinstance(error, asyncio.CancelledError),
    )
