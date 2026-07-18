"""Answer synthesis and direct answer failure classification."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)

PYDANTIC_VALIDATION_FAILED = "answer_synthesis_pydantic_validation_failed"
ANSWER_DRAFT_INVALID = "answer_synthesis_draft_invalid"


class RequestRetryDisposition(StrEnum):
    """Request-local retry decision shared by answer generation failures."""

    RETRY_IN_REQUEST = "retry_in_request"
    DO_NOT_RETRY_IN_REQUEST = "do_not_retry_in_request"
    UNKNOWN = "unknown"


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


def classify_answer_synthesis_failure(
    exc: BaseException,
) -> AnswerSynthesisFailureAttributes:
    """Map answer-synthesis-boundary failures to request-local failure attributes."""

    from app.agent.answering.evidence_answer.contract import (
        EvidenceAnswerDraftGenerationInvalidError,
        EvidenceAnswerDraftInvalidError,
    )

    # FAILURE_MODE を持つのは State/Content の2系統のみ (裸の基底は unknown へ落とす)。
    if isinstance(exc, AIProviderStateError | AIProviderContentError):
        return AnswerSynthesisFailureAttributes(
            code=exc.CODE,
            failure_kind=exc.FAILURE_MODE.value,
            failure_reason=exc.reason.value if exc.reason is not None else None,
            request_retry_disposition=(RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST),
        )
    if isinstance(exc, EvidenceAnswerDraftGenerationInvalidError):
        return AnswerSynthesisFailureAttributes(
            code=exc.defect_code,
            failure_kind="ai_response_invalid",
            failure_reason=exc.defect_code,
            request_retry_disposition=RequestRetryDisposition.RETRY_IN_REQUEST,
        )
    if isinstance(exc, EvidenceAnswerDraftInvalidError):
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
    """Map direct-answer-boundary failures to request-local failure attributes."""

    from app.agent.answering.direct_answer.contract import DirectAnswerInvalidError

    # FAILURE_MODE を持つのは State/Content の2系統のみ (裸の基底は unknown へ落とす)。
    if isinstance(exc, AIProviderStateError | AIProviderContentError):
        return DirectAnswerFailureAttributes(
            code=exc.CODE,
            failure_kind=exc.FAILURE_MODE.value,
            failure_reason=exc.reason.value if exc.reason is not None else None,
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
