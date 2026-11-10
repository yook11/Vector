"""直接回答の工程の生成・再試行・停止・記録(LLMの口と回答の生成の口は偽物)。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.agent.agent import Agent
from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInput,
    DirectAnswerInvalidError,
)
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.answering.direct_answer.service import DirectAnswerService
from app.agent.recording.direct_answer import (
    DirectAnswerFailed,
    DirectAnswerOutcome,
    DirectAnswerStopped,
    DirectAnswerSucceeded,
)
from app.agent.running.state import (
    AttemptSuperseded,
    CannotContinue,
    DeadlineExceeded,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.ai_providers.errors import (
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.http.errors import HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from tests.agent.answering._fakes import ScriptedAnswerGenerationGate
from tests.agent.llm_call._fakes import RecordedLlmCall
from tests.agent.recording._fakes import RecordingDirectAnswerRecorder
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


def _truncated_error() -> AIProviderResultError:
    """S1 streamerが実際に送出する形 (reason付き) を再現する。"""
    return AIProviderResultError(reason=AIProviderResultReason.OUTPUT_TRUNCATED)


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


class ScriptedLlmTextStreamer:
    def __init__(self, outcomes: Sequence[StreamOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[RecordedLlmCall] = []
        self.streams: list[ScriptedAgentTextStream] = []

    def stream_text(
        self,
        agent: Agent[Any, Any],
        input: DirectAnswerInput,
        *,
        attempt_number: int,
    ) -> ScriptedAgentTextStream:
        self.calls.append(
            RecordedLlmCall(
                agent=agent,
                input=input,
                attempt_number=attempt_number,
            )
        )
        outcome = self._outcomes.pop(0)
        stream = ScriptedAgentTextStream(outcome)
        self.streams.append(stream)
        return stream


def _streamer_scope(
    streamer: ScriptedLlmTextStreamer,
) -> Callable[[], AbstractAsyncContextManager[ScriptedLlmTextStreamer]]:
    @asynccontextmanager
    async def scope() -> AsyncIterator[ScriptedLlmTextStreamer]:
        yield streamer

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


def _service(
    streamer: ScriptedLlmTextStreamer,
    *,
    gate: ScriptedAnswerGenerationGate | None = None,
    delta_reporter: RecordingDeltaReporter | None = None,
    recorder: RecordingDirectAnswerRecorder | None = None,
) -> DirectAnswerService:
    answer_gate = ScriptedAnswerGenerationGate() if gate is None else gate
    if recorder is None:
        return DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            llm_text_streamer_scope_factory=_streamer_scope(streamer),
            gate=answer_gate,
            delta_reporter=delta_reporter,
        )
    return DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        llm_text_streamer_scope_factory=_streamer_scope(streamer),
        gate=answer_gate,
        delta_reporter=delta_reporter,
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
    streamer = ScriptedLlmTextStreamer(["検索なしで回答できます。"])
    service = DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        llm_text_streamer_scope_factory=_streamer_scope(streamer),
        gate=ScriptedAnswerGenerationGate(),
    )

    draft = await service.answer(_input())

    assert draft == DirectAnswerDraft(answer="検索なしで回答できます。")
    assert len(streamer.calls) == 1


@pytest.mark.asyncio
async def test_direct_answer_removes_inline_citation_markers_after_generation() -> None:
    streamer = ScriptedLlmTextStreamer(
        ["結論は維持します。[[1]] 詳細は省略します。[[2]]"]
    )

    draft = await DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        llm_text_streamer_scope_factory=_streamer_scope(streamer),
        gate=ScriptedAnswerGenerationGate(),
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

    assert draft == DirectAnswerDraft(answer="結論は維持します。 詳細は省略します。")
    assert streamer.calls[0].input.request.question == "前回の結論だけ"
    assert streamer.calls[0].input.request.history == (
        ThreadMessageSnapshot(role="assistant", content="根拠は説明済み"),
    )
    assert streamer.calls[0].input.previous_answer == "根拠付き前回答 [[1]]"


# --- グループ形 marker 受理 (spec: agent-citation-marker-grouped-refs-slice.md) ---
# direct answerの除去経路も正準形と同様にグループ形を除去対象とする。


@pytest.mark.asyncio
async def test_direct_answer_removes_group_form_citation_markers_after_generation() -> (
    None
):
    """グループ形 [[1], [2]] も正準形と同様に本文から除去される。"""
    streamer = ScriptedLlmTextStreamer(
        ["結論は維持します。[[1], [2]] 詳細は省略します。"]
    )

    draft = await _service(streamer).answer(_input())

    assert draft == DirectAnswerDraft(answer="結論は維持します。 詳細は省略します。")


@pytest.mark.asyncio
async def test_group_form_marker_only_generation_retries_then_raises_invalid() -> None:
    """本文がグループ形markerだけで構成されると除去後は空扱いとなり、
    通常の空回答と同じretry後に工程失敗となる。"""
    streamer = ScriptedLlmTextStreamer(["[[1], [2]]", "[[1], [3]]"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(streamer, delta_reporter=reporter).answer(_input())

    assert isinstance(exc_info.value.__cause__, DirectAnswerInvalidError)
    assert len(streamer.calls) == 2
    assert reporter.aborted == [1, 2]


@pytest.mark.asyncio
async def test_blank_then_valid_retries_once_with_same_input(
    capfire: CaptureLogfire,
) -> None:
    streamer = ScriptedLlmTextStreamer([" \n\t", "再試行後の回答です。"])
    enters = 0
    exits = 0

    @asynccontextmanager
    async def counting_scope() -> AsyncIterator[ScriptedLlmTextStreamer]:
        nonlocal enters, exits
        enters += 1
        try:
            yield streamer
        finally:
            exits += 1

    draft = await DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        llm_text_streamer_scope_factory=counting_scope,
        gate=ScriptedAnswerGenerationGate(),
    ).answer(_input())

    assert draft == DirectAnswerDraft(answer="再試行後の回答です。")
    assert [call.attempt_number for call in streamer.calls] == [1, 2]
    assert streamer.calls[0].input is streamer.calls[1].input
    assert all(call.agent is DIRECT_ANSWER_AGENT for call in streamer.calls)
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
    streamer = ScriptedLlmTextStreamer(["", " \n"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(streamer, delta_reporter=reporter).answer(_input())

    assert exc_info.value.code == "direct_answer_blank_response"
    assert isinstance(exc_info.value.__cause__, DirectAnswerInvalidError)
    assert len(streamer.calls) == 2
    assert reporter.appended == []
    assert reporter.finished == []
    assert reporter.aborted == [1, 2]
    assert all(stream.closed for stream in streamer.streams)

    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "attempt_count": 2,
            "failure_code": "direct_answer_blank_response",
        }
    ]


@pytest.mark.asyncio
async def test_unresolvable_ai_provider_error_becomes_direct_answer_error_without_retry(
    capfire: CaptureLogfire,
) -> None:
    """呼び直しても解消しないAIの失敗は再試行せず、下書きを中断して回答の失敗にする。"""
    provider_exc = AIProviderTransportError(
        http_error=HttpTransportError(
            failure=HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.CERTIFICATE_VERIFICATION,
            )
        )
    )
    streamer = ScriptedLlmTextStreamer([provider_exc])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(streamer, delta_reporter=reporter).answer(_input())

    assert exc_info.value.code == "ai_provider_transport_error"
    assert exc_info.value.__cause__ is provider_exc
    assert len(streamer.calls) == 1
    assert reporter.aborted == [1]
    assert streamer.streams[0].closed is True

    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "attempt_count": 1,
            "failure_code": "ai_provider_transport_error",
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
    streamer = ScriptedLlmTextStreamer([first_error, terminal_error])
    reporter = RecordingDeltaReporter()

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(streamer, delta_reporter=reporter).answer(_input())

    assert exc_info.value.code == "ai_provider_result_error"
    assert exc_info.value.__cause__ is terminal_error
    assert len(streamer.calls) == 2
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == [
        {
            "result": "failed",
            "attempt_count": 2,
            "failure_code": "ai_provider_result_error",
        }
    ]


@pytest.mark.asyncio
async def test_resolvable_ai_failure_retries_once_with_the_same_input() -> None:
    """呼び直せば解消しうるAIの失敗(出力の打ち切り)は同じ入力で1回だけ再試行し、2回目の回答を返す。"""
    streamer = ScriptedLlmTextStreamer([_truncated_error(), "再試行後の回答です。"])

    draft = await _service(streamer).answer(_input())

    assert len(streamer.calls) == 2
    assert streamer.calls[0].input is streamer.calls[1].input
    assert draft == DirectAnswerDraft(answer="再試行後の回答です。")


@pytest.mark.asyncio
async def test_unexpected_exception_propagates_without_outcome(
    capfire: CaptureLogfire,
) -> None:
    unexpected = RuntimeError("boom")
    streamer = ScriptedLlmTextStreamer([unexpected])
    reporter = RecordingDeltaReporter()

    with pytest.raises(RuntimeError) as exc_info:
        await _service(streamer, delta_reporter=reporter).answer(_input())

    assert exc_info.value is unexpected
    assert len(streamer.calls) == 1
    assert reporter.aborted == [1]
    assert streamer.streams[0].closed is True
    metrics = collected_metrics(capfire)
    assert _metric_attributes(metrics, _DIRECT_ANSWER_OUTCOME_METRIC) == []


@pytest.mark.asyncio
async def test_streamer_scope_activation_failure_precedes_attempt_and_observation(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("streamer scope activation failed")
    reporter = RecordingDeltaReporter()

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[Any]:
        raise error
        yield  # pragma: no cover

    with pytest.raises(RuntimeError) as exc_info:
        await DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            llm_text_streamer_scope_factory=broken_scope,
            gate=ScriptedAnswerGenerationGate(),
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
async def test_streamer_scope_exit_failure_discards_completed_outcome(
    capfire: CaptureLogfire,
) -> None:
    error = RuntimeError("streamer scope exit failed")
    streamer = ScriptedLlmTextStreamer(["回答は完成済み"])

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[ScriptedLlmTextStreamer]:
        yield streamer
        raise error

    with pytest.raises(RuntimeError) as exc_info:
        await DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            llm_text_streamer_scope_factory=broken_scope,
            gate=ScriptedAnswerGenerationGate(),
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
async def test_streamer_scope_exit_failure_replaces_terminal_failure_without_outcome(
    capfire: CaptureLogfire,
) -> None:
    source_error = AIProviderTransportError(
        http_error=HttpTransportError(
            failure=HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.CERTIFICATE_VERIFICATION,
            )
        )
    )
    close_error = RuntimeError("streamer scope exit failed")
    streamer = ScriptedLlmTextStreamer([source_error])

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[ScriptedLlmTextStreamer]:
        try:
            yield streamer
        finally:
            raise close_error

    with pytest.raises(RuntimeError) as exc_info:
        await DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            llm_text_streamer_scope_factory=broken_scope,
            gate=ScriptedAnswerGenerationGate(),
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
    streamer = ScriptedLlmTextStreamer(
        [[" \t回答", "[[1", "]] ", "の続き", "です。\n"]]
    )
    reporter = RecordingDeltaReporter()
    gate = ScriptedAnswerGenerationGate()

    draft = await _service(streamer, delta_reporter=reporter, gate=gate).answer(
        _input()
    )

    assert draft == DirectAnswerDraft(answer="回答 の続きです。")
    assert "".join(text for _, text in reporter.appended) == "回答 の続きです。"
    assert {generation for generation, _ in reporter.appended} == {1}
    assert reporter.finished == [1]
    assert reporter.aborted == []

    assert gate.start_calls == 1
    assert gate.regenerate_calls == 0


@pytest.mark.asyncio
async def test_marker_only_blank_generation_retries_without_visible_reset() -> None:
    streamer = ScriptedLlmTextStreamer(
        [
            ["[[", "1]]", " \n\u2003"],
            [" 再", "試行[[2]] ", "回答 "],
        ]
    )
    reporter = RecordingDeltaReporter()

    draft = await _service(streamer, delta_reporter=reporter).answer(_input())

    assert draft == DirectAnswerDraft(answer="再試行 回答")
    generation_two_text = "".join(
        text for generation, text in reporter.appended if generation == 2
    )
    assert generation_two_text == "再試行 回答"
    assert all(generation == 2 for generation, _ in reporter.appended)
    assert reporter.aborted == [1]
    assert reporter.finished == [2]
    assert reporter.reset_calls == 0
    assert all(stream.closed for stream in streamer.streams)


@pytest.mark.parametrize("failing_method", ["append", "finish"])
@pytest.mark.asyncio
async def test_reporter_failure_does_not_change_success(
    failing_method: str,
) -> None:
    streamer = ScriptedLlmTextStreamer([["回答", "です。"]])
    reporter = RecordingDeltaReporter(fail_on=frozenset({failing_method}))

    draft = await _service(streamer, delta_reporter=reporter).answer(_input())

    assert draft == DirectAnswerDraft(answer="回答です。")


@pytest.mark.asyncio
async def test_reporter_abort_failure_does_not_mask_provider_error() -> None:
    provider_exc = AIProviderTransportError(
        http_error=HttpTransportError(
            failure=HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.CERTIFICATE_VERIFICATION,
            )
        )
    )
    streamer = ScriptedLlmTextStreamer([provider_exc])
    reporter = RecordingDeltaReporter(fail_on=frozenset({"abort"}))

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(streamer, delta_reporter=reporter).answer(_input())

    assert exc_info.value.__cause__ is provider_exc
    assert reporter.aborted == [1]


async def test_successful_answer_records_succeeded_outcome() -> None:
    """完成 draft は recorder へ成功結論を1回渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    streamer = ScriptedLlmTextStreamer(["検索なしで回答できます。"])

    await _service(streamer, recorder=recorder).answer(_input())

    _assert_recorded(recorder, outcome=DirectAnswerSucceeded(attempt_count=1))


async def test_classified_failure_records_failed_outcome() -> None:
    """分類済み失敗は recorder へ失敗結論を1回渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    error = AIProviderTransportError(
        http_error=HttpTransportError(
            failure=HttpTransportFailure(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.CERTIFICATE_VERIFICATION,
            )
        )
    )
    streamer = ScriptedLlmTextStreamer([error])

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(streamer, recorder=recorder).answer(_input())

    assert exc_info.value.code == "ai_provider_transport_error"
    assert exc_info.value.__cause__ is error
    _assert_recorded(
        recorder,
        outcome=DirectAnswerFailed(
            failure_code="ai_provider_transport_error",
            attempt_count=1,
        ),
        error=exc_info.value,
    )


async def test_retry_then_success_records_attempt_count() -> None:
    """retry後の成功は実際の試行回数を渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    streamer = ScriptedLlmTextStreamer([" \n\t", "再試行後の回答です。"])

    await _service(streamer, recorder=recorder).answer(_input())

    _assert_recorded(recorder, outcome=DirectAnswerSucceeded(attempt_count=2))


async def test_retry_then_classified_failure_records_attempt_count() -> None:
    """retry後の分類済み失敗は実際の試行回数を渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    streamer = ScriptedLlmTextStreamer(["", " "])

    with pytest.raises(DirectAnswerError) as exc_info:
        await _service(streamer, recorder=recorder).answer(_input())

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


async def test_regeneration_stop_returns_reason_and_records_stopped_outcome() -> None:
    """書き直す前にRunが止まると、止まった理由を返し、記録係へ止まった結論を渡す。"""

    recorder = RecordingDirectAnswerRecorder()
    streamer = ScriptedLlmTextStreamer([" "])

    result = await _service(
        streamer,
        gate=ScriptedAnswerGenerationGate(regenerations=(AttemptSuperseded(),)),
        recorder=recorder,
    ).answer(_input())

    assert result == AttemptSuperseded()
    _assert_recorded(recorder, outcome=DirectAnswerStopped())


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
    streamer = ScriptedLlmTextStreamer([error])

    with pytest.raises(type(error)) as exc_info:
        await _service(streamer, recorder=recorder).answer(_input())

    assert exc_info.value is error
    _assert_recorded(recorder, outcome=None, error=error)


@pytest.mark.asyncio
async def test_generation_starts_only_after_the_gate_allows_the_start() -> None:
    """回答の生成の口が開始を許してから、最初の生成を始める。"""
    timeline: list[str] = []
    streamer = ScriptedLlmTextStreamer(["開始後の回答"])
    original_stream = streamer.stream_text

    def tracked_stream(*args: object, **kwargs: object) -> ScriptedAgentTextStream:
        timeline.append("generate")
        return original_stream(*args, **kwargs)

    streamer.stream_text = tracked_stream  # type: ignore[method-assign]

    draft = await _service(
        streamer,
        gate=ScriptedAnswerGenerationGate(timeline=timeline),
    ).answer(_input())

    assert draft == DirectAnswerDraft(answer="開始後の回答")
    assert timeline == ["answer_start", "generate"]


def _unused_scope(
    streamer: ScriptedLlmTextStreamer, entered: list[int]
) -> Callable[[], AbstractAsyncContextManager[ScriptedLlmTextStreamer]]:
    @asynccontextmanager
    async def scope() -> AsyncIterator[ScriptedLlmTextStreamer]:
        entered.append(1)
        yield streamer

    return scope


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [DeadlineExceeded(), AttemptSuperseded()])
async def test_start_stop_returns_reason_without_generating_or_recording(
    stopped: CannotContinue,
) -> None:
    """開始の前にRunが止まると、止まった理由を返し、生成も記録係も開かない。"""
    streamer = ScriptedLlmTextStreamer(["呼ばれない"])
    entered: list[int] = []
    gate = ScriptedAnswerGenerationGate(start=stopped)
    recorder = RecordingDirectAnswerRecorder()

    result = await DirectAnswerService(
        agent=DIRECT_ANSWER_AGENT,
        llm_text_streamer_scope_factory=_unused_scope(streamer, entered),
        gate=gate,
        recorder=recorder,
    ).answer(_input())

    assert (result, gate.start_calls, entered, streamer.calls, recorder.records) == (
        stopped,
        1,
        [],
        [],
        [],
    )


@pytest.mark.asyncio
async def test_start_failure_propagates_without_generating_or_recording() -> None:
    """開始の問い合わせが失敗すると、同じ例外を送出し、生成も記録係も開かない。"""
    failure = RuntimeError("answer start transaction failed")
    streamer = ScriptedLlmTextStreamer(["呼ばれない"])
    entered: list[int] = []
    gate = ScriptedAnswerGenerationGate(start=failure)
    recorder = RecordingDirectAnswerRecorder()

    with pytest.raises(RuntimeError) as raised:
        await DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            llm_text_streamer_scope_factory=_unused_scope(streamer, entered),
            gate=gate,
            recorder=recorder,
        ).answer(_input())

    assert raised.value is failure
    assert (gate.start_calls, entered, streamer.calls, recorder.records) == (
        1,
        [],
        [],
        [],
    )


@pytest.mark.asyncio
async def test_regeneration_stop_does_not_start_second_generation() -> None:
    """書き直す前にRunが止まると、止まった理由を返し、2回目の生成を始めない。"""
    streamer = ScriptedLlmTextStreamer([" \n\t", "呼ばれない"])
    gate = ScriptedAnswerGenerationGate(regenerations=(DeadlineExceeded(),))

    result = await _service(streamer, gate=gate).answer(_input())

    assert result == DeadlineExceeded()
    assert [call.attempt_number for call in streamer.calls] == [1]
    assert gate.regenerate_calls == 1


@pytest.mark.asyncio
async def test_regeneration_keeps_single_answer_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = 0
    original_timeout = asyncio.timeout

    def counting_timeout(delay: float) -> object:
        nonlocal created
        created += 1
        return original_timeout(delay)

    monkeypatch.setattr(asyncio, "timeout", counting_timeout)
    streamer = ScriptedLlmTextStreamer([" \n\t", "再試行後の回答です。"])

    draft = await _service(streamer).answer(_input())

    assert draft == DirectAnswerDraft(answer="再試行後の回答です。")
    assert created == 1
    assert [call.attempt_number for call in streamer.calls] == [1, 2]
