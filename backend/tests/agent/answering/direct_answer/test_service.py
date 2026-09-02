"""Direct answer service tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

import app.agent.answering.direct_answer.contract as direct_answer_contract
import app.agent.contract as shared_agent_contract
from app.agent.agent import Agent
from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.direct_answer.contract import (
    AnswerGenerationStopped,
    DirectAnswerDraft,
    DirectAnswerInput,
    DirectAnswerInvalidError,
)
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.answering.direct_answer.service import DirectAnswerService
from app.agent.recording.direct_answer import (
    DirectAnswerFailed,
    DirectAnswerOutcome,
    DirectAnswerSucceeded,
)
from app.agent.runs.execution import Continue, Stop, StopReason
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderNetworkError,
    AIProviderOutputTruncatedError,
)
from app.analysis.gemini_error_translator import GeminiStateReason
from tests.agent.recording._fakes import RecordingDirectAnswerRecorder
from tests.agent.runtime._fakes import AgentRuntimeCall
from tests.logfire._metric_helpers import collected_metrics

_DIRECT_ANSWER_OUTCOME_METRIC = "vector.agent.direct_answer.outcome"
_DIRECT_ANSWER_DURATION_METRIC = "vector.agent.direct_answer.duration"


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


def _input(
    request: AnsweringRequest | None = None,
    *,
    previous_answer: str = "",
) -> DirectAnswerInput:
    return DirectAnswerInput(
        request=_request() if request is None else request,
        previous_answer=previous_answer,
    )


StreamOutcome = str | Sequence[str] | BaseException


class ScriptedAgentTextStream:
    def __init__(self, outcome: StreamOutcome) -> None:
        if isinstance(outcome, BaseException):
            self._items: list[str | BaseException] = [outcome]
        elif isinstance(outcome, str):
            self._items = [outcome]
        else:
            self._items = list(outcome)
        self.closed = False

    def __aiter__(self) -> ScriptedAgentTextStream:
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


class ScriptedStreamingRuntime:
    def __init__(self, outcomes: Sequence[StreamOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[AgentRuntimeCall] = []
        self.streams: list[ScriptedAgentTextStream] = []

    def stream_text(
        self,
        agent: Agent[Any, Any],
        input: DirectAnswerInput,
        *,
        attempt_number: int,
    ) -> ScriptedAgentTextStream:
        self.calls.append(
            AgentRuntimeCall(
                agent=agent,
                input=input,
                attempt_number=attempt_number,
            )
        )
        outcome = self._outcomes.pop(0)
        stream = ScriptedAgentTextStream(outcome)
        self.streams.append(stream)
        return stream


def _runtime_scope(
    runtime: ScriptedStreamingRuntime,
) -> Callable[[], AbstractAsyncContextManager[ScriptedStreamingRuntime]]:
    @asynccontextmanager
    async def scope() -> AsyncIterator[ScriptedStreamingRuntime]:
        yield runtime

    return scope


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
    def __init__(self, results: Sequence[Continue | Stop]) -> None:
        self._results = list(results)
        self.calls = 0

    async def should_continue(self) -> Continue | Stop:
        self.calls += 1
        if not self._results:
            return Continue()
        return self._results.pop(0)


def test_answer_generation_stopped_is_shared_identity_compatible_reexport() -> None:
    assert (
        direct_answer_contract.AnswerGenerationStopped
        is shared_agent_contract.AnswerGenerationStopped
    )


def _service(
    runtime: ScriptedStreamingRuntime,
    *,
    delta_reporter: RecordingDeltaReporter | None = None,
    continuation: SequenceContinuation | None = None,
    recorder: RecordingDirectAnswerRecorder | None = None,
) -> DirectAnswerService:
    if recorder is None:
        return DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=_runtime_scope(runtime),
            delta_reporter=delta_reporter,
            continuation=continuation,
        )
    return DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=_runtime_scope(runtime),
        delta_reporter=delta_reporter,
        continuation=continuation,
        recorder=recorder,
    )


def _assert_recorded(
    recorder: RecordingDirectAnswerRecorder,
    *,
    outcome: DirectAnswerOutcome | None,
    error: BaseException | None = None,
) -> None:
    assert len(recorder.records) == 1
    recorded = recorder.records[0]
    assert recorded.agent_name == DIRECT_ANSWER_AGENT.name
    assert recorded.outcomes == ([] if outcome is None else [outcome])
    assert recorded.error is error


@pytest.mark.asyncio
async def test_valid_text_returns_direct_draft_without_retry() -> None:
    runtime = ScriptedStreamingRuntime(["検索なしで回答できます。"])
    service = DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=_runtime_scope(runtime),
    )

    draft = await service.answer(_input())

    assert draft == DirectAnswerDraft(answer="検索なしで回答できます。")
    assert len(runtime.calls) == 1


@pytest.mark.asyncio
async def test_direct_answer_removes_inline_citation_markers_after_generation() -> None:
    runtime = ScriptedStreamingRuntime(
        ["結論は維持します。[[1]] 詳細は省略します。[[2]]"]
    )

    draft = await DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=_runtime_scope(runtime),
    ).answer(
        _input(
            AnsweringRequest(
                question="前回の結論だけ",
                history=(
                    ThreadMessageSnapshot(role="assistant", content="根拠は説明済み"),
                ),
                as_of=_as_of(),
            ),
            previous_answer="根拠付き前回答 [[1]]",
        )
    )

    assert draft.answer == "結論は維持します。 詳細は省略します。"
    assert runtime.calls[0].input.request.question == "前回の結論だけ"
    assert runtime.calls[0].input.request.history == (
        ThreadMessageSnapshot(role="assistant", content="根拠は説明済み"),
    )
    assert runtime.calls[0].input.previous_answer == "根拠付き前回答 [[1]]"


# --- グループ形 marker 受理 (spec: agent-citation-marker-grouped-refs-slice.md) ---
# direct answerの除去経路も正準形と同様にグループ形を除去対象とする。


@pytest.mark.asyncio
async def test_direct_answer_removes_group_form_citation_markers_after_generation() -> (
    None
):
    """グループ形 [[1], [2]] も正準形と同様に本文から除去される。"""
    runtime = ScriptedStreamingRuntime(
        ["結論は維持します。[[1], [2]] 詳細は省略します。"]
    )

    draft = await _service(runtime).answer(_input())

    assert draft.answer == "結論は維持します。 詳細は省略します。"


@pytest.mark.asyncio
async def test_group_form_marker_only_generation_retries_then_raises_invalid() -> None:
    """本文がグループ形markerだけで構成されると除去後は空扱いとなり、
    通常の空回答と同じretry後に工程失敗となる。"""
    runtime = ScriptedStreamingRuntime(["[[1], [2]]", "[[1], [3]]"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(runtime, delta_reporter=reporter).answer(_input())

    assert isinstance(exc_info.value.__cause__, DirectAnswerInvalidError)
    assert len(runtime.calls) == 2
    assert reporter.aborted == [1, 2]


@pytest.mark.asyncio
async def test_blank_then_valid_retries_once_with_same_input(
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedStreamingRuntime([" \n\t", "再試行後の回答です。"])
    enters = 0
    exits = 0

    @asynccontextmanager
    async def counting_scope() -> AsyncIterator[ScriptedStreamingRuntime]:
        nonlocal enters, exits
        enters += 1
        try:
            yield runtime
        finally:
            exits += 1

    draft = await DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        runtime_scope_factory=counting_scope,
    ).answer(_input())

    assert draft.answer == "再試行後の回答です。"
    assert [call.attempt_number for call in runtime.calls] == [1, 2]
    assert runtime.calls[0].input is runtime.calls[1].input
    assert all(call.agent is DIRECT_ANSWER_AGENT for call in runtime.calls)
    assert enters == 1
    assert exits == 1
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "succeeded",
            "attempt_count": 2,
            "failure_code": "none",
        }
    ]


@pytest.mark.asyncio
async def test_blank_twice_raises_invalid_after_observation(
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedStreamingRuntime(["", " \n"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(runtime, delta_reporter=reporter).answer(_input())

    assert exc_info.value.code == "direct_answer_blank_response"
    assert isinstance(exc_info.value.__cause__, DirectAnswerInvalidError)
    assert len(runtime.calls) == 2
    assert reporter.appended == []
    assert reporter.finished == []
    assert reporter.aborted == [1, 2]
    assert all(stream.closed for stream in runtime.streams)

    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "attempt_count": 2,
            "failure_code": "direct_answer_blank_response",
        }
    ]


@pytest.mark.asyncio
async def test_ai_provider_error_becomes_direct_answer_error_without_retry(
    capfire: CaptureLogfire,
) -> None:
    provider_exc = AIProviderNetworkError()
    runtime = ScriptedStreamingRuntime([provider_exc])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(runtime, delta_reporter=reporter).answer(_input())

    assert exc_info.value.code == "ai_error_network"
    assert exc_info.value.__cause__ is provider_exc
    assert len(runtime.calls) == 1
    assert reporter.aborted == [1]
    assert runtime.streams[0].closed is True

    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "attempt_count": 1,
            "failure_code": "ai_error_network",
        }
    ]


@pytest.mark.asyncio
async def test_second_truncation_raises_classified_truncation_error_after_retry(
    capfire: CaptureLogfire,
) -> None:
    """1回目のMAX_TOKENSはrequest内でretryされ、2回目も打ち切られたら分類済み
    errorが呼び出し元へ送出される。"""
    first_error = _truncated_error()
    terminal_error = _truncated_error()
    runtime = ScriptedStreamingRuntime([first_error, terminal_error])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(runtime, delta_reporter=reporter).answer(_input())

    assert exc_info.value.code == "ai_error_output_truncated"
    assert exc_info.value.__cause__ is terminal_error
    assert len(runtime.calls) == 2
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "attempt_count": 2,
            "failure_code": "ai_error_output_truncated",
        }
    ]


@pytest.mark.asyncio
async def test_truncated_first_attempt_retries_with_truncation_flag() -> None:
    """R2条件6(flow層): 打ち切りはrequest内でretryされ、2回目の入力に

    previous_output_truncated=Trueが立つ (Evidence側S2と同じ受け渡し)。
    """
    runtime = ScriptedStreamingRuntime([_truncated_error(), "再試行後の回答です。"])

    draft = await _service(runtime).answer(_input())

    assert len(runtime.calls) == 2
    assert runtime.calls[0].input.previous_output_truncated is False
    assert runtime.calls[1].input.previous_output_truncated is True
    assert draft.answer == "再試行後の回答です。"


@pytest.mark.asyncio
async def test_blank_retry_does_not_carry_truncation_flag() -> None:
    """R2条件7(flow層): 打ち切り以外(空回答)が原因のretryでは

    previous_output_truncatedを立てない。
    """
    runtime = ScriptedStreamingRuntime([" \n\t", "再試行後の回答です。"])

    draft = await _service(runtime).answer(_input())

    assert len(runtime.calls) == 2
    assert runtime.calls[0].input is runtime.calls[1].input
    assert runtime.calls[0].input.previous_output_truncated is False
    assert runtime.calls[1].input.previous_output_truncated is False
    assert draft.answer == "再試行後の回答です。"


@pytest.mark.asyncio
async def test_unexpected_exception_propagates_without_outcome(
    capfire: CaptureLogfire,
) -> None:
    unexpected = RuntimeError("boom")
    runtime = ScriptedStreamingRuntime([unexpected])
    reporter = RecordingDeltaReporter()

    with pytest.raises(RuntimeError) as exc_info:
        await _service(runtime, delta_reporter=reporter).answer(_input())

    assert exc_info.value is unexpected
    assert len(runtime.calls) == 1
    assert reporter.aborted == [1]
    assert runtime.streams[0].closed is True
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
        await DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=broken_scope,
            delta_reporter=reporter,
        ).answer(_input())

    assert exc_info.value is error
    assert reporter.appended == []
    assert reporter.finished == []
    assert reporter.aborted == []
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == []
    assert _metric_attributes(
        metrics,
        _DIRECT_ANSWER_DURATION_METRIC,
    ) == [
        {
            "status": "failed",
            "outcome": "none",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_scope_exit_failure_discards_completed_outcome(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("runtime scope exit failed")
    runtime = ScriptedStreamingRuntime(["回答は完成済み"])

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[ScriptedStreamingRuntime]:
        yield runtime
        raise error

    with pytest.raises(RuntimeError) as exc_info:
        await DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=broken_scope,
        ).answer(_input())

    assert exc_info.value is error
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == []
    assert _metric_attributes(metrics, _DIRECT_ANSWER_DURATION_METRIC) == [
        {
            "status": "failed",
            "outcome": "none",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_scope_exit_failure_replaces_terminal_failure_without_outcome(
    capfire: CaptureLogfire,
) -> None:
    source_error = AIProviderNetworkError()
    close_error = RuntimeError("runtime scope exit failed")
    runtime = ScriptedStreamingRuntime([source_error])

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[ScriptedStreamingRuntime]:
        try:
            yield runtime
        finally:
            raise close_error

    with pytest.raises(RuntimeError) as exc_info:
        await DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=broken_scope,
        ).answer(_input())

    assert exc_info.value is close_error
    direct_answer_error = close_error.__context__
    assert isinstance(direct_answer_error, DirectAnswerError)
    assert direct_answer_error.__cause__ is source_error
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == []
    assert _metric_attributes(metrics, _DIRECT_ANSWER_DURATION_METRIC) == [
        {
            "status": "failed",
            "outcome": "none",
        }
    ]


@pytest.mark.asyncio
async def test_incremental_fragments_reconstruct_existing_final_answer() -> None:
    runtime = ScriptedStreamingRuntime(
        [[" \t回答", "[[1", "]] ", "の続き", "です。\n"]]
    )
    reporter = RecordingDeltaReporter()

    draft = await _service(runtime, delta_reporter=reporter).answer(_input())

    assert draft == DirectAnswerDraft(answer="回答 の続きです。")
    assert "".join(text for _, text in reporter.appended) == draft.answer
    assert {generation for generation, _ in reporter.appended} == {1}
    assert reporter.finished == [1]
    assert reporter.aborted == []


@pytest.mark.asyncio
async def test_marker_only_blank_generation_retries_without_visible_reset() -> None:
    runtime = ScriptedStreamingRuntime(
        [
            ["[[", "1]]", " \n\u2003"],
            [" 再", "試行[[2]] ", "回答 "],
        ]
    )
    reporter = RecordingDeltaReporter()

    draft = await _service(runtime, delta_reporter=reporter).answer(_input())

    assert draft == DirectAnswerDraft(answer="再試行 回答")
    generation_two_text = "".join(
        text for generation, text in reporter.appended if generation == 2
    )
    assert generation_two_text == draft.answer
    assert all(generation == 2 for generation, _ in reporter.appended)
    assert reporter.aborted == [1]
    assert reporter.finished == [2]
    assert reporter.reset_calls == 0
    assert all(stream.closed for stream in runtime.streams)


@pytest.mark.parametrize("failing_method", ["append", "finish"])
@pytest.mark.asyncio
async def test_reporter_failure_does_not_change_success(
    failing_method: str,
) -> None:
    runtime = ScriptedStreamingRuntime([["回答", "です。"]])
    reporter = RecordingDeltaReporter(fail_on=frozenset({failing_method}))

    draft = await _service(runtime, delta_reporter=reporter).answer(_input())

    assert draft == DirectAnswerDraft(answer="回答です。")


@pytest.mark.asyncio
async def test_reporter_abort_failure_does_not_mask_provider_error() -> None:
    provider_exc = AIProviderNetworkError()
    runtime = ScriptedStreamingRuntime([provider_exc])
    reporter = RecordingDeltaReporter(fail_on=frozenset({"abort"}))

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(runtime, delta_reporter=reporter).answer(_input())

    assert exc_info.value.__cause__ is provider_exc
    assert reporter.aborted == [1]


@pytest.mark.asyncio
async def test_continuation_false_before_provider_start_is_routine_stop() -> None:
    assert not issubclass(AnswerGenerationStopped, AIProviderError)
    assert not issubclass(AnswerGenerationStopped, DirectAnswerInvalidError)
    runtime = ScriptedStreamingRuntime(["呼ばれない"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(AnswerGenerationStopped):
        await _service(
            runtime,
            delta_reporter=reporter,
            continuation=SequenceContinuation([Stop(StopReason.NOT_CURRENT)]),
        ).answer(_input())

    assert runtime.calls == []
    assert runtime.streams == []
    assert reporter.appended == []
    assert reporter.aborted == [1]
    assert reporter.finished == []


@pytest.mark.asyncio
async def test_continuation_false_mid_stream_aborts_iterator_and_pending_report() -> (
    None
):
    runtime = ScriptedStreamingRuntime([["表示済み", "見せない本文"]])
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation(
        [Continue(), Continue(), Stop(StopReason.NOT_CURRENT)]
    )

    with pytest.raises(AnswerGenerationStopped):
        await _service(
            runtime,
            delta_reporter=reporter,
            continuation=continuation,
        ).answer(_input())

    assert continuation.calls == 3
    assert "".join(text for _, text in reporter.appended) == "表示済み"
    assert reporter.aborted == [1]
    assert reporter.finished == []
    assert runtime.streams[0].closed is True


@pytest.mark.asyncio
async def test_continuation_false_at_normal_stream_end_aborts_before_finish(
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedStreamingRuntime([["表示済み本文"]])
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation(
        [Continue(), Continue(), Stop(StopReason.NOT_CURRENT)]
    )

    with pytest.raises(AnswerGenerationStopped):
        await _service(
            runtime,
            delta_reporter=reporter,
            continuation=continuation,
        ).answer(_input())

    assert continuation.calls == 3
    assert reporter.appended == [(1, "表示済み本文")]
    assert reporter.aborted == [1]
    assert reporter.finished == []
    assert runtime.streams[0].closed is True
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == []


async def test_successful_answer_records_succeeded_outcome() -> None:
    """完成 draft は recorder へ成功結論を1回渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    runtime = ScriptedStreamingRuntime(["検索なしで回答できます。"])

    await _service(runtime, recorder=recorder).answer(_input())

    _assert_recorded(recorder, outcome=DirectAnswerSucceeded(attempt_count=1))


async def test_classified_failure_records_failed_outcome() -> None:
    """分類済み失敗は recorder へ失敗結論を1回渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    error = AIProviderNetworkError()
    runtime = ScriptedStreamingRuntime([error])

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(runtime, recorder=recorder).answer(_input())

    assert exc_info.value.code == "ai_error_network"
    assert exc_info.value.__cause__ is error
    _assert_recorded(
        recorder,
        outcome=DirectAnswerFailed(
            failure_code="ai_error_network",
            attempt_count=1,
        ),
        error=exc_info.value,
    )


async def test_retry_then_success_records_attempt_count() -> None:
    """retry後の成功は実際の試行回数を渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    runtime = ScriptedStreamingRuntime([" \n\t", "再試行後の回答です。"])

    await _service(runtime, recorder=recorder).answer(_input())

    _assert_recorded(recorder, outcome=DirectAnswerSucceeded(attempt_count=2))


async def test_retry_then_classified_failure_records_attempt_count() -> None:
    """retry後の分類済み失敗は実際の試行回数を渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    runtime = ScriptedStreamingRuntime(["", " "])

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(runtime, recorder=recorder).answer(_input())

    assert exc_info.value.code == "direct_answer_blank_response"
    assert isinstance(exc_info.value.__cause__, DirectAnswerInvalidError)
    _assert_recorded(
        recorder,
        outcome=DirectAnswerFailed(
            failure_code="direct_answer_blank_response",
            attempt_count=2,
        ),
        error=exc_info.value,
    )


async def test_generation_stop_records_stopped_without_outcome() -> None:
    """AnswerGenerationStopped は結論を渡さず同一インスタンスで伝播する。"""

    recorder = RecordingDirectAnswerRecorder()
    runtime = ScriptedStreamingRuntime(["呼ばれない"])

    with pytest.raises(AnswerGenerationStopped) as exc_info:
        await _service(
            runtime,
            continuation=SequenceContinuation([Stop(StopReason.NOT_CURRENT)]),
            recorder=recorder,
        ).answer(_input())

    _assert_recorded(recorder, outcome=None, error=exc_info.value)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("boom"), id="unknown"),
        pytest.param(asyncio.CancelledError(), id="cancellation"),
        pytest.param(GeneratorExit(), id="generator-exit"),
    ],
)
async def test_unclassified_failure_and_stops_record_without_outcome(
    error: BaseException,
) -> None:
    """未分類例外と停止は結論を渡さず同一インスタンスで伝播する。"""

    recorder = RecordingDirectAnswerRecorder()
    runtime = ScriptedStreamingRuntime([error])

    with pytest.raises(type(error)) as exc_info:
        await _service(runtime, recorder=recorder).answer(_input())

    assert exc_info.value is error
    _assert_recorded(recorder, outcome=None, error=error)
