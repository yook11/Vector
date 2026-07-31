"""Answer synthesis and direct answer failure classification tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraftInvalidError,
)
from app.agent.answering.failure import (
    ANSWER_DRAFT_INVALID,
    PYDANTIC_VALIDATION_FAILED,
    AnswerSynthesisFailureAttributes,
    DirectAnswerFailureAttributes,
    RequestRetryDisposition,
    classify_answer_synthesis_failure,
    classify_direct_answer_failure,
)
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderOutputTruncatedError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.gemini_error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)


class _ValidationProbe(BaseModel):
    value: int


_ClassifierResult = AnswerSynthesisFailureAttributes | DirectAnswerFailureAttributes
_Classifier = Callable[[BaseException], _ClassifierResult]
_CLASSIFIERS: tuple[_Classifier, ...] = (
    classify_answer_synthesis_failure,
    classify_direct_answer_failure,
)
_CLASSIFIER_IDS = ["answer_synthesis", "direct_answer"]


def _truncated_error() -> AIProviderOutputTruncatedError:
    """S1 runtimeが実際に送出する形 (reason付き) を再現する。"""
    return AIProviderOutputTruncatedError(
        reason=GeminiStateReason.OUTPUT_TOKEN_LIMIT_REACHED
    )


@pytest.mark.parametrize("classify", _CLASSIFIERS, ids=_CLASSIFIER_IDS)
def test_output_truncated_error_is_retried_in_request(classify: _Classifier) -> None:
    """打ち切りだけがrequest内retry対象になる。

    spec: 打ち切りはrequest内でretryする。
    """
    exc = _truncated_error()

    attrs = classify(exc)

    assert exc.reason is not None
    assert attrs.code == exc.CODE
    assert attrs.failure_kind == exc.FAILURE_MODE.value
    assert attrs.failure_reason == exc.reason.value
    assert attrs.request_retry_disposition is RequestRetryDisposition.RETRY_IN_REQUEST


@pytest.mark.parametrize("classify", _CLASSIFIERS, ids=_CLASSIFIER_IDS)
@pytest.mark.parametrize(
    "exc",
    [
        AIProviderNetworkError(),
        AIProviderRateLimitedError(),
        AIProviderConfigurationError(),
        AIProviderUsageLimitExhaustedError(),
        AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
    ],
    ids=[
        "network_attempt_scoped",
        "rate_limited_time_based_recovery",
        "configuration_operator_action_required",
        "usage_limit_condition_based_recovery",
        "output_blocked_content_error",
    ],
)
def test_other_provider_errors_stay_do_not_retry_in_request(
    classify: _Classifier,
    exc: BaseException,
) -> None:
    """打ち切り以外のState/Content errorの分類は不変 (regression guard)。

    AIProviderNetworkErrorはAIProviderOutputTruncatedErrorと同じ
    FAILURE_MODE (ATTEMPT_SCOPED) を持つため、disposition判定がFAILURE_MODEだけを
    見て型を見ない実装ならここが誤って RETRY_IN_REQUEST になり検知できる。
    """
    attrs = classify(exc)

    assert (
        attrs.request_retry_disposition
        is RequestRetryDisposition.DO_NOT_RETRY_IN_REQUEST
    )


def test_evidence_draft_invalid_error_is_retried_in_request() -> None:
    """R3条件12(回帰ガード): EvidenceAnswerDraftInvalidError(現役、finalizeが

    citation不整合で送出する)の分類はEvidenceAnswerDraftGenerationInvalidError
    撤去後も維持される。
    """
    exc = EvidenceAnswerDraftInvalidError("unknown citation ref: 9")

    attrs = classify_answer_synthesis_failure(exc)

    assert attrs.code == ANSWER_DRAFT_INVALID
    assert attrs.failure_kind == "ai_response_invalid"
    assert attrs.failure_reason == "unknown citation ref: 9"
    assert attrs.request_retry_disposition is RequestRetryDisposition.RETRY_IN_REQUEST


def test_validation_error_is_retried_in_request() -> None:
    """R3条件12(回帰ガード): pydantic ValidationError(構築防御ゲートとして残す

    判断が確定している)の分類はEvidenceAnswerDraftGenerationInvalidError
    撤去後も維持される。
    """
    try:
        _ValidationProbe.model_validate({"value": "not-an-int"})
    except ValidationError as raised:
        exc = raised
    else:
        raise AssertionError("_ValidationProbe must reject a non-integer value")

    attrs = classify_answer_synthesis_failure(exc)

    assert attrs.code == PYDANTIC_VALIDATION_FAILED
    assert attrs.failure_kind == "ai_response_invalid"
    assert attrs.failure_reason == PYDANTIC_VALIDATION_FAILED
    assert attrs.request_retry_disposition is RequestRetryDisposition.RETRY_IN_REQUEST


def test_unclassified_exception_falls_back_to_unknown() -> None:
    """R3条件12(回帰ガード): 未分類例外のunknown fallbackは

    EvidenceAnswerDraftGenerationInvalidError撤去後も維持される。
    """
    attrs = classify_answer_synthesis_failure(RuntimeError("boom"))

    assert attrs.code == "unexpected_error"
    assert attrs.failure_kind == "unknown"
    assert attrs.failure_reason is None
    assert attrs.request_retry_disposition is RequestRetryDisposition.UNKNOWN
