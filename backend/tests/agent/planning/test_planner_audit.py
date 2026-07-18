"""Question planner audit value / mapper tests."""

from __future__ import annotations

from app.agent.planning import audit as planner_audit
from app.agent.planning.audit import (
    PlannerAttemptFailureEvent,
    PlannerDraftReceivedEvent,
    PlannerFinalEvent,
    PlannerOutcomeCode,
    RequestRetryDisposition,
    classify_planner_failure,
)
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.analysis.ai_provider_errors import AIProviderNetworkError


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


def test_each_response_defect_maps_to_shared_invalid_response_contract() -> None:
    for defect in AgentResponseDefect:
        attrs = classify_planner_failure(
            AgentResponseInvalidError(
                defect,
                repair_hint="REPAIR_HINT_MUST_NOT_ENTER_AUDIT_2a91",
            )
        )

        assert attrs.code == defect.value
        assert attrs.failure_kind == "ai_response_invalid"
        assert attrs.failure_reason == defect.value
        assert (
            attrs.request_retry_disposition is RequestRetryDisposition.RETRY_IN_REQUEST
        )
        assert "REPAIR_HINT_MUST_NOT_ENTER_AUDIT_2a91" not in str(
            attrs.model_dump(mode="json")
        )


def test_unknown_error_is_not_treated_as_response_invalid() -> None:
    attrs = classify_planner_failure(ValueError("arbitrary validation-like failure"))

    assert attrs.code == "unexpected_error"
    assert attrs.failure_kind == "unknown"
    assert attrs.failure_reason is None
    assert attrs.request_retry_disposition is RequestRetryDisposition.UNKNOWN
    assert not hasattr(planner_audit, "PYDANTIC_VALIDATION_FAILED")


def test_attempt_failure_event_has_no_raw_question_prompt_or_response() -> None:
    event = PlannerAttemptFailureEvent.from_failure(
        attempt_number=1,
        failure=classify_planner_failure(
            AgentResponseInvalidError(
                AgentResponseDefect.RESPONSE_NOT_OBJECT,
                repair_hint="REPAIR_HINT_SENTINEL_29af",
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
    assert payload["failure_reason"] == "response_not_object"
    dumped = str(payload)
    for needle in (
        "ユーザー質問",
        "prompt text",
        "raw_response",
        "NVIDIA latest",
        "REPAIR_HINT_SENTINEL_29af",
    ):
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
