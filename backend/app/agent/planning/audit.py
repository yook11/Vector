"""Question planner audit values and failure classification."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agent.contract import RetrievalMode
from app.agent.runtime.contract import AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderError


class RequestRetryDisposition(StrEnum):
    """Request-local retry decision for planner failures."""

    RETRY_IN_REQUEST = "retry_in_request"
    DO_NOT_RETRY_IN_REQUEST = "do_not_retry_in_request"
    UNKNOWN = "unknown"


class PlannerOutcomeCode(StrEnum):
    """Planner audit outcome codes."""

    ATTEMPT_FAILED = "question_plan_attempt_failed"
    DRAFT_RECEIVED = "question_plan_draft_received"
    PLAN_CREATED = "question_plan_created"
    FALLBACK_USED = "question_plan_fallback_used"


class PlannerFailureAttributes(BaseModel):
    """Failure attributes carried from an attempt failure to final fallback."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    failure_kind: str = Field(min_length=1)
    failure_reason: str | None = None
    request_retry_disposition: RequestRetryDisposition


class PlannerAttemptFailureEvent(BaseModel):
    """Planner attempt-level failure event."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["agent_planner"] = "agent_planner"
    outcome_code: PlannerOutcomeCode = PlannerOutcomeCode.ATTEMPT_FAILED
    attempt_number: int = Field(ge=1)
    request_retry_disposition: RequestRetryDisposition
    failure_kind: str = Field(min_length=1)
    failure_reason: str | None = None
    code: str = Field(min_length=1)
    ai_model: str | None = None
    prompt_version: str | None = None

    @classmethod
    def from_failure(
        cls,
        *,
        attempt_number: int,
        failure: PlannerFailureAttributes,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> PlannerAttemptFailureEvent:
        return cls(
            attempt_number=attempt_number,
            request_retry_disposition=failure.request_retry_disposition,
            failure_kind=failure.failure_kind,
            failure_reason=failure.failure_reason,
            code=failure.code,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )


class PlannerDraftReceivedEvent(BaseModel):
    """Planner draft-level success event recorded before plan construction."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["agent_planner"] = "agent_planner"
    outcome_code: PlannerOutcomeCode = PlannerOutcomeCode.DRAFT_RECEIVED
    attempt_number: int = Field(ge=1)
    retrieval_mode: RetrievalMode
    draft_internal_query_count: int = Field(ge=0)
    draft_external_query_count: int = Field(ge=0)
    ai_model: str | None = None
    prompt_version: str | None = None


class PlannerFinalEvent(BaseModel):
    """Planner final result event."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["agent_planner"] = "agent_planner"
    outcome_code: PlannerOutcomeCode
    attempt_count: int = Field(ge=1)
    retry_used: bool
    fallback_used: bool
    retrieval_mode: RetrievalMode
    internal_query_count: int = Field(ge=0)
    external_query_count: int = Field(ge=0)
    request_retry_disposition: RequestRetryDisposition | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None
    code: str | None = None
    ai_model: str | None = None
    prompt_version: str | None = None

    @classmethod
    def plan_created(
        cls,
        *,
        attempt_count: int,
        retry_used: bool,
        retrieval_mode: RetrievalMode,
        internal_query_count: int,
        external_query_count: int,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> PlannerFinalEvent:
        return cls(
            outcome_code=PlannerOutcomeCode.PLAN_CREATED,
            attempt_count=attempt_count,
            retry_used=retry_used,
            fallback_used=False,
            retrieval_mode=retrieval_mode,
            internal_query_count=internal_query_count,
            external_query_count=external_query_count,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )

    @classmethod
    def fallback(
        cls,
        *,
        attempt_count: int,
        retry_used: bool,
        retrieval_mode: RetrievalMode,
        internal_query_count: int,
        external_query_count: int,
        failure: PlannerFailureAttributes,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> PlannerFinalEvent:
        return cls(
            outcome_code=PlannerOutcomeCode.FALLBACK_USED,
            attempt_count=attempt_count,
            retry_used=retry_used,
            fallback_used=True,
            retrieval_mode=retrieval_mode,
            internal_query_count=internal_query_count,
            external_query_count=external_query_count,
            request_retry_disposition=failure.request_retry_disposition,
            failure_kind=failure.failure_kind,
            failure_reason=failure.failure_reason,
            code=failure.code,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )


class PlannerAuditRecorder(Protocol):
    """Best-effort sink for planner audit events."""

    async def record_draft_received(
        self,
        event: PlannerDraftReceivedEvent,
    ) -> None: ...

    async def record_attempt_failure(
        self,
        event: PlannerAttemptFailureEvent,
    ) -> None: ...

    async def record_final_event(self, event: PlannerFinalEvent) -> None: ...


def classify_planner_failure(exc: BaseException) -> PlannerFailureAttributes:
    """Map planner-boundary failures to request-local audit attributes."""

    if isinstance(exc, AIProviderError):
        reason = getattr(exc, "reason", None)
        return PlannerFailureAttributes(
            code=exc.CODE,
            failure_kind=exc.FAILURE_MODE.value,
            failure_reason=reason.value if reason is not None else None,
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
