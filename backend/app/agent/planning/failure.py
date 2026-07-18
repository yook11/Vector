"""Question planner failure classification."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.agent.runtime.contract import AgentResponseInvalidError
from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)


class RequestRetryDisposition(StrEnum):
    """Request-local retry decision for planner failures."""

    RETRY_IN_REQUEST = "retry_in_request"
    DO_NOT_RETRY_IN_REQUEST = "do_not_retry_in_request"
    UNKNOWN = "unknown"


class PlannerFailureAttributes(BaseModel):
    """Failure attributes carried from an attempt failure to final fallback."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    failure_kind: str = Field(min_length=1)
    failure_reason: str | None = None
    request_retry_disposition: RequestRetryDisposition


def classify_planner_failure(exc: BaseException) -> PlannerFailureAttributes:
    """Map planner-boundary failures to request-local failure attributes."""

    # FAILURE_MODE を持つのは State/Content の2系統のみ (裸の基底は unknown へ落とす)。
    if isinstance(exc, AIProviderStateError | AIProviderContentError):
        return PlannerFailureAttributes(
            code=exc.CODE,
            failure_kind=exc.FAILURE_MODE.value,
            failure_reason=exc.reason.value if exc.reason is not None else None,
            request_retry_disposition=(RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST),
        )
    if isinstance(exc, AgentResponseInvalidError):
        return PlannerFailureAttributes(
            code=exc.defect.value,
            failure_kind="ai_response_invalid",
            failure_reason=exc.defect.value,
            request_retry_disposition=RequestRetryDisposition.RETRY_IN_REQUEST,
        )
    return PlannerFailureAttributes(
        code="unexpected_error",
        failure_kind="unknown",
        failure_reason=None,
        request_retry_disposition=RequestRetryDisposition.UNKNOWN,
    )
