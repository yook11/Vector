"""Answer synthesis and direct answer audit values/failure classification."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.contract import RetrievalMode
from app.analysis.ai_provider_errors import AIProviderError

PYDANTIC_VALIDATION_FAILED = "answer_synthesis_pydantic_validation_failed"
ANSWER_DRAFT_INVALID = "answer_synthesis_draft_invalid"
DIRECT_ANSWER_BLANK_RESPONSE = "direct_answer_blank_response"


class RequestRetryDisposition(StrEnum):
    """Request-local retry decision shared by answer generation failures."""

    RETRY_IN_REQUEST = "retry_in_request"
    DO_NOT_RETRY_IN_REQUEST = "do_not_retry_in_request"
    UNKNOWN = "unknown"


class AnswerSynthesisOutcomeCode(StrEnum):
    """Answer synthesis audit outcome codes."""

    ATTEMPT_FAILED = "answer_synthesis_attempt_failed"
    DEFECT_COMPLETED = "answer_synthesis_defect_completed"
    SYNTHESIZED = "answer_synthesis_synthesized"
    FALLBACK_USED = "answer_synthesis_fallback_used"


class DirectAnswerOutcomeCode(StrEnum):
    """Direct answer audit outcome codes."""

    ATTEMPT_FAILED = "direct_answer_attempt_failed"
    ANSWERED = "direct_answer_answered"
    FAILED = "direct_answer_failed"


class AnswerSynthesisFailureAttributes(BaseModel):
    """Failure attributes carried from an attempt failure to final fallback."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    failure_kind: str = Field(min_length=1)
    failure_reason: str | None = None
    request_retry_disposition: RequestRetryDisposition


class DirectAnswerFailureAttributes(BaseModel):
    """Failure attributes carried from a direct attempt failure to final failure."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    failure_kind: str = Field(min_length=1)
    failure_reason: str | None = None
    request_retry_disposition: RequestRetryDisposition


class AnswerSynthesisAttemptFailureEvent(BaseModel):
    """Answer synthesis attempt-level failure event."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["agent_answer_synthesis"] = "agent_answer_synthesis"
    outcome_code: AnswerSynthesisOutcomeCode = AnswerSynthesisOutcomeCode.ATTEMPT_FAILED
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
        failure: AnswerSynthesisFailureAttributes,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> AnswerSynthesisAttemptFailureEvent:
        return cls(
            attempt_number=attempt_number,
            request_retry_disposition=failure.request_retry_disposition,
            failure_kind=failure.failure_kind,
            failure_reason=failure.failure_reason,
            code=failure.code,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )


class DirectAnswerAttemptFailureEvent(BaseModel):
    """Direct answer attempt-level failure event."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["agent_direct_answer"] = "agent_direct_answer"
    outcome_code: DirectAnswerOutcomeCode = DirectAnswerOutcomeCode.ATTEMPT_FAILED
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
        failure: DirectAnswerFailureAttributes,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> DirectAnswerAttemptFailureEvent:
        return cls(
            attempt_number=attempt_number,
            request_retry_disposition=failure.request_retry_disposition,
            failure_kind=failure.failure_kind,
            failure_reason=failure.failure_reason,
            code=failure.code,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )


class AnswerSynthesisDefectEvent(BaseModel):
    """Answer synthesis deterministic completion event."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["agent_answer_synthesis"] = "agent_answer_synthesis"
    outcome_code: AnswerSynthesisOutcomeCode = (
        AnswerSynthesisOutcomeCode.DEFECT_COMPLETED
    )
    attempt_number: int = Field(ge=1)
    defect_code: str = Field(min_length=1)
    ai_model: str | None = None
    prompt_version: str | None = None


class AnswerSynthesisFinalEvent(BaseModel):
    """Answer synthesis final result event."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["agent_answer_synthesis"] = "agent_answer_synthesis"
    outcome_code: AnswerSynthesisOutcomeCode
    attempt_count: int = Field(ge=1)
    retry_used: bool
    fallback_used: bool
    status: Literal["answered", "insufficient"]
    evidence_count: int = Field(ge=0)
    cited_ref_count: int = Field(ge=0)
    missing_aspect_count: int = Field(ge=0)
    defect_count: int = Field(ge=0)
    planned_retrieval_mode: RetrievalMode | Literal["unknown"] = "unknown"
    request_retry_disposition: RequestRetryDisposition | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None
    code: str | None = None
    ai_model: str | None = None
    prompt_version: str | None = None

    @classmethod
    def synthesized(
        cls,
        *,
        attempt_count: int,
        retry_used: bool,
        status: Literal["answered", "insufficient"],
        evidence_count: int,
        cited_ref_count: int,
        missing_aspect_count: int,
        defect_count: int,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> AnswerSynthesisFinalEvent:
        return cls(
            outcome_code=AnswerSynthesisOutcomeCode.SYNTHESIZED,
            attempt_count=attempt_count,
            retry_used=retry_used,
            fallback_used=False,
            status=status,
            evidence_count=evidence_count,
            cited_ref_count=cited_ref_count,
            missing_aspect_count=missing_aspect_count,
            defect_count=defect_count,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )

    @classmethod
    def fallback(
        cls,
        *,
        attempt_count: int,
        retry_used: bool,
        draft_status: Literal["answered", "insufficient"],
        evidence_count: int,
        cited_ref_count: int,
        missing_aspect_count: int,
        defect_count: int,
        failure: AnswerSynthesisFailureAttributes,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> AnswerSynthesisFinalEvent:
        return cls(
            outcome_code=AnswerSynthesisOutcomeCode.FALLBACK_USED,
            attempt_count=attempt_count,
            retry_used=retry_used,
            fallback_used=True,
            status=draft_status,
            evidence_count=evidence_count,
            cited_ref_count=cited_ref_count,
            missing_aspect_count=missing_aspect_count,
            defect_count=defect_count,
            request_retry_disposition=failure.request_retry_disposition,
            failure_kind=failure.failure_kind,
            failure_reason=failure.failure_reason,
            code=failure.code,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )


class DirectAnswerFinalEvent(BaseModel):
    """Direct answer final result event."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["agent_direct_answer"] = "agent_direct_answer"
    outcome_code: DirectAnswerOutcomeCode
    attempt_count: int = Field(ge=1)
    retry_used: bool
    request_retry_disposition: RequestRetryDisposition | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None
    code: str | None = None
    ai_model: str | None = None
    prompt_version: str | None = None

    @classmethod
    def answered(
        cls,
        *,
        attempt_count: int,
        retry_used: bool,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> DirectAnswerFinalEvent:
        return cls(
            outcome_code=DirectAnswerOutcomeCode.ANSWERED,
            attempt_count=attempt_count,
            retry_used=retry_used,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )

    @classmethod
    def failed(
        cls,
        *,
        attempt_count: int,
        retry_used: bool,
        failure: DirectAnswerFailureAttributes,
        ai_model: str | None,
        prompt_version: str | None,
    ) -> DirectAnswerFinalEvent:
        return cls(
            outcome_code=DirectAnswerOutcomeCode.FAILED,
            attempt_count=attempt_count,
            retry_used=retry_used,
            request_retry_disposition=failure.request_retry_disposition,
            failure_kind=failure.failure_kind,
            failure_reason=failure.failure_reason,
            code=failure.code,
            ai_model=ai_model,
            prompt_version=prompt_version,
        )


class AnswerSynthesisAuditRecorder(Protocol):
    """Best-effort sink for answer synthesis audit events."""

    async def record_attempt_failure(
        self,
        event: AnswerSynthesisAttemptFailureEvent,
    ) -> None: ...

    async def record_defect(self, event: AnswerSynthesisDefectEvent) -> None: ...

    async def record_final_event(self, event: AnswerSynthesisFinalEvent) -> None: ...


class DirectAnswerAuditRecorder(Protocol):
    """Best-effort sink for direct answer audit events."""

    async def record_attempt_failure(
        self,
        event: DirectAnswerAttemptFailureEvent,
    ) -> None: ...

    async def record_final_event(self, event: DirectAnswerFinalEvent) -> None: ...


def classify_answer_synthesis_failure(
    exc: BaseException,
) -> AnswerSynthesisFailureAttributes:
    """Map answer-synthesis-boundary failures to request-local audit attributes."""

    from app.agent.answering.synthesis import (
        AnswerDraftGenerationInvalidError,
        AnswerDraftInvalidError,
    )

    if isinstance(exc, AIProviderError):
        reason = getattr(exc, "reason", None)
        return AnswerSynthesisFailureAttributes(
            code=exc.CODE,
            failure_kind=exc.FAILURE_MODE.value,
            failure_reason=reason.value if reason is not None else None,
            request_retry_disposition=(RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST),
        )
    if isinstance(exc, AnswerDraftGenerationInvalidError):
        return AnswerSynthesisFailureAttributes(
            code=exc.defect_code,
            failure_kind="ai_response_invalid",
            failure_reason=exc.defect_code,
            request_retry_disposition=RequestRetryDisposition.RETRY_IN_REQUEST,
        )
    if isinstance(exc, AnswerDraftInvalidError):
        return AnswerSynthesisFailureAttributes(
            code=ANSWER_DRAFT_INVALID,
            failure_kind="ai_response_invalid",
            failure_reason=str(exc) or ANSWER_DRAFT_INVALID,
            request_retry_disposition=RequestRetryDisposition.RETRY_IN_REQUEST,
        )
    if isinstance(exc, ValidationError):
        return AnswerSynthesisFailureAttributes(
            code=PYDANTIC_VALIDATION_FAILED,
            failure_kind="ai_response_invalid",
            failure_reason=PYDANTIC_VALIDATION_FAILED,
            request_retry_disposition=RequestRetryDisposition.RETRY_IN_REQUEST,
        )
    return AnswerSynthesisFailureAttributes(
        code="unexpected_error",
        failure_kind="unknown",
        failure_reason=None,
        request_retry_disposition=RequestRetryDisposition.UNKNOWN,
    )


def classify_direct_answer_failure(
    exc: BaseException,
) -> DirectAnswerFailureAttributes:
    """Map direct-answer-boundary failures to request-local audit attributes."""

    from app.agent.answering.direct import DirectAnswerInvalidError

    if isinstance(exc, AIProviderError):
        reason = getattr(exc, "reason", None)
        return DirectAnswerFailureAttributes(
            code=exc.CODE,
            failure_kind=exc.FAILURE_MODE.value,
            failure_reason=reason.value if reason is not None else None,
            request_retry_disposition=(RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST),
        )
    if isinstance(exc, DirectAnswerInvalidError):
        return DirectAnswerFailureAttributes(
            code=exc.code,
            failure_kind="ai_response_invalid",
            failure_reason=exc.code,
            request_retry_disposition=RequestRetryDisposition.RETRY_IN_REQUEST,
        )
    return DirectAnswerFailureAttributes(
        code="unexpected_error",
        failure_kind="unknown",
        failure_reason=None,
        request_retry_disposition=RequestRetryDisposition.UNKNOWN,
    )
