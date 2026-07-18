"""Question planner failure classification tests."""

from __future__ import annotations

from app.agent.planning import failure as planner_failure
from app.agent.planning.failure import (
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
    assert not hasattr(planner_failure, "PYDANTIC_VALIDATION_FAILED")
