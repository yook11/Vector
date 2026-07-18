"""Direct answer flow tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInvalidError,
)
from app.agent.answering.direct_answer.flow import DirectAnswerFlow
from app.agent.question_context.contract import AnswerRequirement, QuestionContext
from app.analysis.ai_provider_errors import AIProviderError, AIProviderNetworkError
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


def _request() -> AnsweringRequest:
    return AnsweringRequest(
        context=QuestionContext(
            standalone_question="Vector の使い方を短く教えて",
            content_requirements=[
                AnswerRequirement(
                    requirement_id="c1",
                    description="Vector の使い方を説明する",
                )
            ],
            response_requirements=[
                AnswerRequirement(
                    requirement_id="p1",
                    description="短く回答する",
                )
            ],
            relevant_prior_coverage="前回は基本操作を説明済み",
            active_goal="Vector を使い始める",
        ),
        as_of=_as_of(),
    )


StreamOutcome = str | Sequence[str] | Exception


class FakeDirectAnswerStream:
    def __init__(self, outcome: StreamOutcome) -> None:
        if isinstance(outcome, Exception):
            self._items: list[str | Exception] = [outcome]
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
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


class FakeDirectAnswerGenerator:
    model_name = "fake-direct-model"
    prompt_version = "direct0001"

    def __init__(self, outcomes: Sequence[StreamOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeDirectAnswerStream] = []

    def stream(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",
        previous_error: str | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "request": request,
                "previous_answer": previous_answer,
                "previous_error": previous_error,
            }
        )
        outcome = self._outcomes.pop(0)
        stream = FakeDirectAnswerStream(outcome)
        self.streams.append(stream)
        return stream


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


def _answer_generation_stopped_type() -> type[BaseException]:
    contract = import_module("app.agent.answering.direct_answer.contract")
    stopped_type = getattr(contract, "AnswerGenerationStopped", None)
    assert stopped_type is not None, "AnswerGenerationStopped が未実装です"
    assert isinstance(stopped_type, type) and issubclass(stopped_type, BaseException)
    return stopped_type


def test_answer_generation_stopped_is_shared_identity_compatible_reexport() -> None:
    shared_contract = import_module("app.agent.contract")
    direct_contract = import_module("app.agent.answering.direct_answer.contract")

    shared_type = getattr(shared_contract, "AnswerGenerationStopped", None)
    direct_type = getattr(direct_contract, "AnswerGenerationStopped", None)

    assert shared_type is not None, "shared AnswerGenerationStopped が未実装です"
    assert direct_type is shared_type


async def _answer(
    generator: FakeDirectAnswerGenerator,
    *,
    delta_reporter: RecordingDeltaReporter | None = None,
    continuation: SequenceContinuation | None = None,
) -> DirectAnswerDraft:
    return await DirectAnswerFlow(
        generator=generator,
        delta_reporter=delta_reporter,
        continuation=continuation,
    ).answer(
        request=_request(),
        previous_answer="",
    )


@pytest.mark.asyncio
async def test_valid_text_returns_direct_draft_without_retry(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeDirectAnswerGenerator(["検索なしで回答できます。"])
    reporter = RecordingDeltaReporter()

    draft = await _answer(generator, delta_reporter=reporter)

    assert draft == DirectAnswerDraft(answer="検索なしで回答できます。")
    assert len(generator.calls) == 1
    assert generator.calls[0]["previous_error"] is None
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

    draft = await DirectAnswerFlow(generator=generator).answer(
        request=AnsweringRequest(
            context=QuestionContext(
                standalone_question="前回の結論だけ",
                content_requirements=[
                    AnswerRequirement(
                        requirement_id="c1",
                        description="前回の結論を説明する",
                    )
                ],
                response_requirements=[
                    AnswerRequirement(
                        requirement_id="p1",
                        description="結論だけを短く回答する",
                    )
                ],
                relevant_prior_coverage="根拠は説明済み",
                active_goal="投資判断を進める",
            ),
            as_of=_as_of(),
        ),
        previous_answer="根拠付き前回答 [[1]]",
    )

    assert draft.answer == "結論は維持します。 詳細は省略します。"
    assert (
        generator.calls[0]["request"].context.content_requirements[0].description
        == "前回の結論を説明する"
    )
    assert (
        generator.calls[0]["request"].context.response_requirements[0].description
        == "結論だけを短く回答する"
    )
    assert (
        generator.calls[0]["request"].context.relevant_prior_coverage
        == "根拠は説明済み"
    )
    assert generator.calls[0]["request"].context.active_goal == "投資判断を進める"
    assert generator.calls[0]["previous_answer"] == "根拠付き前回答 [[1]]"


@pytest.mark.asyncio
async def test_blank_then_valid_retries_once_with_previous_error(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeDirectAnswerGenerator([" \n\t", "再試行後の回答です。"])

    draft = await _answer(generator)

    assert draft.answer == "再試行後の回答です。"
    assert [call["previous_error"] for call in generator.calls] == [
        None,
        "direct_answer_blank_response",
    ]
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
    stopped_type = _answer_generation_stopped_type()
    assert not issubclass(stopped_type, AIProviderError)
    assert not issubclass(stopped_type, DirectAnswerInvalidError)
    generator = FakeDirectAnswerGenerator(["呼ばれない"])
    reporter = RecordingDeltaReporter()

    with pytest.raises(stopped_type):
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
    stopped_type = _answer_generation_stopped_type()
    generator = FakeDirectAnswerGenerator([["表示済み", "見せない本文"]])
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation([True, True, False])

    with pytest.raises(stopped_type):
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
    stopped_type = _answer_generation_stopped_type()
    generator = FakeDirectAnswerGenerator([["表示済み本文"]])
    reporter = RecordingDeltaReporter()
    continuation = SequenceContinuation([True, True, False])

    with pytest.raises(stopped_type):
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
