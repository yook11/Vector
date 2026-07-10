"""Direct answer flow tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.agent.answering.audit import (
    DirectAnswerAttemptFailureEvent,
    DirectAnswerFinalEvent,
    DirectAnswerOutcomeCode,
    RequestRetryDisposition,
)
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerInvalidError,
)
from app.agent.answering.direct_answer.flow import DirectAnswerFlow
from app.analysis.ai_provider_errors import AIProviderNetworkError
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_DIRECT_ANSWER_OUTCOME_METRIC = "vector.agent.direct_answer.outcome"


def _as_of() -> datetime:
    return datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


class FakeDirectAnswerGenerator:
    model_name = "fake-direct-model"
    prompt_version = "direct0001"

    def __init__(self, outcomes: Sequence[str | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        *,
        question: str,
        as_of: datetime,
        user_intent: str = "",
        user_activity_context: str = "",
        previous_answer: str = "",
        previous_error: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "question": question,
                "as_of": as_of,
                "user_intent": user_intent,
                "user_activity_context": user_activity_context,
                "previous_answer": previous_answer,
                "previous_error": previous_error,
            }
        )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeDirectAnswerAuditRecorder:
    def __init__(self) -> None:
        self.attempt_failures: list[DirectAnswerAttemptFailureEvent] = []
        self.final_events: list[DirectAnswerFinalEvent] = []

    async def record_attempt_failure(
        self,
        event: DirectAnswerAttemptFailureEvent,
    ) -> None:
        self.attempt_failures.append(event)

    async def record_final_event(self, event: DirectAnswerFinalEvent) -> None:
        self.final_events.append(event)


class RaisingDirectAnswerAuditRecorder:
    async def record_attempt_failure(
        self,
        event: DirectAnswerAttemptFailureEvent,
    ) -> None:
        raise RuntimeError("audit recorder down")

    async def record_final_event(self, event: DirectAnswerFinalEvent) -> None:
        raise RuntimeError("audit recorder down")


async def _answer(
    generator: FakeDirectAnswerGenerator,
    *,
    recorder: FakeDirectAnswerAuditRecorder | RaisingDirectAnswerAuditRecorder | None,
) -> DirectAnswerDraft:
    return await DirectAnswerFlow(
        generator=generator,
        audit_recorder=recorder,
    ).answer(
        question="Vector の使い方を短く教えて",
        as_of=_as_of(),
    )


@pytest.mark.asyncio
async def test_valid_text_returns_direct_draft_without_retry(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeDirectAnswerGenerator(["検索なしで回答できます。"])
    recorder = FakeDirectAnswerAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft == DirectAnswerDraft(answer="検索なしで回答できます。")
    assert len(generator.calls) == 1
    assert generator.calls[0]["previous_error"] is None
    assert recorder.attempt_failures == []
    assert len(recorder.final_events) == 1
    final = recorder.final_events[0]
    assert final.outcome_code is DirectAnswerOutcomeCode.ANSWERED
    assert final.attempt_count == 1
    assert final.retry_used is False
    assert final.ai_model == "fake-direct-model"
    assert final.prompt_version == "direct0001"

    metrics = collected_metrics(capfire)
    assert (
        sum_counter_for_result(metrics, _DIRECT_ANSWER_OUTCOME_METRIC, "answered") == 1
    )


@pytest.mark.asyncio
async def test_direct_answer_removes_inline_citation_markers_after_generation() -> None:
    generator = FakeDirectAnswerGenerator(
        ["結論は維持します。[[1]] 詳細は省略します。[[2]]"]
    )

    draft = await DirectAnswerFlow(generator=generator).answer(
        question="前回の結論だけ",
        as_of=_as_of(),
        user_intent="結論だけを短く",
        user_activity_context="投資判断を調査中",
        previous_answer="根拠付き前回答 [[1]]",
    )

    assert draft.answer == "結論は維持します。 詳細は省略します。"
    assert generator.calls[0]["user_intent"] == "結論だけを短く"
    assert generator.calls[0]["user_activity_context"] == "投資判断を調査中"
    assert generator.calls[0]["previous_answer"] == "根拠付き前回答 [[1]]"


@pytest.mark.asyncio
async def test_blank_then_valid_retries_once_with_previous_error() -> None:
    generator = FakeDirectAnswerGenerator([" \n\t", "再試行後の回答です。"])
    recorder = FakeDirectAnswerAuditRecorder()

    draft = await _answer(generator, recorder=recorder)

    assert draft.answer == "再試行後の回答です。"
    assert [call["previous_error"] for call in generator.calls] == [
        None,
        "direct_answer_blank_response",
    ]
    assert [event.attempt_number for event in recorder.attempt_failures] == [1]
    attempt = recorder.attempt_failures[0]
    assert attempt.failure_kind == "ai_response_invalid"
    assert attempt.request_retry_disposition is RequestRetryDisposition.RETRY_IN_REQUEST
    assert len(recorder.final_events) == 1
    final = recorder.final_events[0]
    assert final.outcome_code is DirectAnswerOutcomeCode.ANSWERED
    assert final.attempt_count == 2
    assert final.retry_used is True


@pytest.mark.asyncio
async def test_blank_twice_raises_invalid_after_observation(
    capfire: CaptureLogfire,
) -> None:
    generator = FakeDirectAnswerGenerator(["", " \n"])
    recorder = FakeDirectAnswerAuditRecorder()

    with pytest.raises(DirectAnswerInvalidError):
        await _answer(generator, recorder=recorder)

    assert len(generator.calls) == 2
    assert [event.attempt_number for event in recorder.attempt_failures] == [1, 2]
    assert {event.request_retry_disposition for event in recorder.attempt_failures} == {
        RequestRetryDisposition.RETRY_IN_REQUEST
    }
    assert len(recorder.final_events) == 1
    final = recorder.final_events[0]
    assert final.outcome_code is DirectAnswerOutcomeCode.FAILED
    assert final.attempt_count == 2
    assert final.retry_used is True
    assert final.failure_kind == "ai_response_invalid"
    assert final.code == "direct_answer_blank_response"

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _DIRECT_ANSWER_OUTCOME_METRIC, "failed") == 1


@pytest.mark.asyncio
async def test_ai_provider_error_propagates_unwrapped_without_retry() -> None:
    provider_exc = AIProviderNetworkError()
    generator = FakeDirectAnswerGenerator([provider_exc])
    recorder = FakeDirectAnswerAuditRecorder()

    with pytest.raises(AIProviderNetworkError) as exc_info:
        await _answer(generator, recorder=recorder)

    assert exc_info.value is provider_exc
    assert len(generator.calls) == 1
    assert [event.attempt_number for event in recorder.attempt_failures] == [1]
    attempt = recorder.attempt_failures[0]
    assert attempt.request_retry_disposition is (
        RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST
    )
    assert attempt.failure_kind == provider_exc.FAILURE_MODE.value
    assert attempt.code == provider_exc.CODE
    assert len(recorder.final_events) == 1
    final = recorder.final_events[0]
    assert final.outcome_code is DirectAnswerOutcomeCode.FAILED
    assert final.retry_used is False
    assert final.failure_kind == provider_exc.FAILURE_MODE.value


@pytest.mark.asyncio
async def test_unexpected_exception_propagates_without_observation(
    capfire: CaptureLogfire,
) -> None:
    unexpected = RuntimeError("boom")
    generator = FakeDirectAnswerGenerator([unexpected])
    recorder = FakeDirectAnswerAuditRecorder()

    with pytest.raises(RuntimeError) as exc_info:
        await _answer(generator, recorder=recorder)

    assert exc_info.value is unexpected
    assert len(generator.calls) == 1
    assert recorder.attempt_failures == []
    assert recorder.final_events == []
    metrics = collected_metrics(capfire)
    assert (
        sum_counter_for_result(metrics, _DIRECT_ANSWER_OUTCOME_METRIC, "answered") == 0
    )
    assert sum_counter_for_result(metrics, _DIRECT_ANSWER_OUTCOME_METRIC, "failed") == 0


@pytest.mark.asyncio
async def test_audit_recorder_failure_does_not_mask_success() -> None:
    generator = FakeDirectAnswerGenerator(["監査が落ちても回答は返します。"])

    draft = await _answer(generator, recorder=RaisingDirectAnswerAuditRecorder())

    assert draft.answer == "監査が落ちても回答は返します。"


@pytest.mark.asyncio
async def test_audit_recorder_failure_does_not_mask_typed_failure() -> None:
    provider_exc = AIProviderNetworkError()
    generator = FakeDirectAnswerGenerator([provider_exc])

    with pytest.raises(AIProviderNetworkError) as exc_info:
        await _answer(generator, recorder=RaisingDirectAnswerAuditRecorder())

    assert exc_info.value is provider_exc
