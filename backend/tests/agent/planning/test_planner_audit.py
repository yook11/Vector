"""Question planner audit value / mapper tests."""

from __future__ import annotations

from pydantic import ValidationError

from app.agent.planning.ai.gemini import (
    GeminiQuestionPlannerResponseDefect,
    QuestionPlannerResponseInvalidError,
)
from app.agent.planning.audit import (
    PlannerAttemptFailureEvent,
    PlannerDraftReceivedEvent,
    PlannerFinalEvent,
    PlannerOutcomeCode,
    RequestRetryDisposition,
    classify_planner_failure,
)
from app.agent.planning.plan_draft import QuestionPlanDraft
from app.analysis.ai_provider_errors import AIProviderNetworkError


def _validation_error() -> ValidationError:
    try:
        QuestionPlanDraft(retrieval_mode="none", reason="")
    except ValidationError as exc:
        return exc
    raise AssertionError("expected validation error")


def test_provider_error_maps_to_request_local_do_not_retry() -> None:
    attrs = classify_planner_failure(AIProviderNetworkError())

    assert attrs.code == "ai_error_network"
    assert attrs.failure_kind == "attempt_scoped"
    assert attrs.failure_reason is None
    assert (
        attrs.request_retry_disposition
        is RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST
    )
    dumped = attrs.model_dump(mode="json")
    assert "Recoverable" not in str(dumped)
    assert "Terminal" not in str(dumped)


def test_response_invalid_maps_defect_to_failure_reason() -> None:
    attrs = classify_planner_failure(
        QuestionPlannerResponseInvalidError(
            GeminiQuestionPlannerResponseDefect.NOT_JSON
        )
    )

    assert attrs.code == GeminiQuestionPlannerResponseDefect.NOT_JSON.value
    assert attrs.failure_kind == "ai_response_invalid"
    assert attrs.failure_reason == "question_planner_response_gemini_not_json"
    assert attrs.request_retry_disposition is RequestRetryDisposition.RETRY_IN_REQUEST


def test_pydantic_validation_error_maps_to_response_invalid() -> None:
    attrs = classify_planner_failure(_validation_error())

    assert attrs.code == "question_planner_response_pydantic_validation_failed"
    assert attrs.failure_kind == "ai_response_invalid"
    assert (
        attrs.failure_reason == "question_planner_response_pydantic_validation_failed"
    )
    assert attrs.request_retry_disposition is RequestRetryDisposition.RETRY_IN_REQUEST


def test_attempt_failure_event_has_no_raw_question_prompt_or_response() -> None:
    event = PlannerAttemptFailureEvent.from_failure(
        attempt_number=1,
        failure=classify_planner_failure(
            QuestionPlannerResponseInvalidError(
                GeminiQuestionPlannerResponseDefect.NOT_OBJECT
            )
        ),
        ai_model="gemini-2.5-flash",
        prompt_version="question-planner-gemini-v1",
    )

    payload = event.model_dump(mode="json")
    assert event.outcome_code is PlannerOutcomeCode.ATTEMPT_FAILED
    assert payload["kind"] == "agent_planner"
    assert payload["attempt_number"] == 1
    assert payload["failure_kind"] == "ai_response_invalid"
    assert payload["failure_reason"] == "question_planner_response_gemini_not_object"
    dumped = str(payload)
    for needle in ("ユーザー質問", "prompt text", "raw_response", "NVIDIA latest"):
        assert needle not in dumped


def test_final_event_records_counts_not_query_text() -> None:
    event = PlannerFinalEvent.plan_created(
        attempt_count=2,
        retry_used=True,
        retrieval_mode="internal_and_external",
        internal_query_count=2,
        external_query_count=1,
        ai_model="gemini-2.5-flash",
        prompt_version="question-planner-gemini-v1",
    )

    payload = event.model_dump(mode="json")
    assert event.outcome_code is PlannerOutcomeCode.PLAN_CREATED
    assert payload["attempt_count"] == 2
    assert payload["retry_used"] is True
    assert payload["fallback_used"] is False
    assert payload["internal_query_count"] == 2
    assert payload["external_query_count"] == 1
    dumped = str(payload)
    for needle in ("NVIDIA", "OpenAI", "query text", "raw_response"):
        assert needle not in dumped


def test_draft_received_event_records_raw_counts_not_query_text() -> None:
    event = PlannerDraftReceivedEvent(
        attempt_number=2,
        retrieval_mode="internal_and_external",
        draft_internal_query_count=5,
        draft_external_query_count=4,
        ai_model="gemini-2.5-flash",
        prompt_version="question-planner-gemini-v1",
    )

    payload = event.model_dump(mode="json")
    assert event.outcome_code is PlannerOutcomeCode.DRAFT_RECEIVED
    assert payload["attempt_number"] == 2
    assert payload["retrieval_mode"] == "internal_and_external"
    assert payload["draft_internal_query_count"] == 5
    assert payload["draft_external_query_count"] == 4
    dumped = str(payload)
    for needle in ("NVIDIA", "OpenAI", "query text", "raw_response"):
        assert needle not in dumped
