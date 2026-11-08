"""精査の工程の失敗の型。原因から自由文を含まないcodeを名乗ることを確かめる(外部依存なし)。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.evidence_review.failure import EvidenceReviewError
from app.agent.evidence_review.preparation import EvidenceReviewInput
from app.agent.evidence_review.selection import EvidenceReviewerResponse
from app.agent.evidence_review.service import _review_attempt
from app.agent.output import AgentOutputDefect, AgentOutputInvalidError
from app.ai_providers.errors import (
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.http.errors import HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from tests.agent.evidence_review._builders import AS_OF
from tests.agent.llm_call._fakes import ScriptedLlmCaller


@pytest.mark.parametrize(
    ("cause", "expected_code"),
    [
        pytest.param(
            AgentOutputInvalidError(
                AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH,
                repair_hint="SECRET_REPAIR_HINT_MUST_NOT_LEAK",
            ),
            "output_schema_mismatch",
            id="output-defect",
        ),
        pytest.param(
            AIProviderTransportError(
                http_error=HttpTransportError(
                    failure=HttpTransportFailure(
                        HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                    )
                )
            ),
            "timeout",
            id="provider-reason",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_BLOCKED_SAFETY),
            "output_blocked_safety",
            id="provider-content-reason",
        ),
        pytest.param(TimeoutError("SECRET_TIMEOUT_MESSAGE"), "reviewer_timeout"),
    ],
)
def test_evidence_review_error_names_the_cause_as_safe_code(
    cause: (
        AgentOutputInvalidError
        | AIProviderTransportError
        | AIProviderResultError
        | TimeoutError
    ),
    expected_code: str,
) -> None:
    """原因を保持し、原因から自由文を含まないcodeを名乗ってログ用のdetailsにも載せる。"""
    error = EvidenceReviewError(cause=cause)

    assert (
        error.cause is cause,
        error.__cause__ is cause,
        error.code,
        error.details,
    ) == (True, True, expected_code, {"code": expected_code})
    assert "SECRET" not in str(error)


def test_evidence_review_error_names_draft_validation_error_as_schema_mismatch() -> (
    None
):
    """精査結果の検証に失敗した原因は、出力の形の不一致として名乗る。"""
    with pytest.raises(ValidationError) as raised:
        EvidenceReviewerResponse.from_raw(
            selections=[{"option_index": 0, "claim": "", "why_selected": "w"}],
            missing=[],
        )

    error = EvidenceReviewError(cause=raised.value)

    assert error.code == "output_schema_mismatch"


@pytest.mark.asyncio
async def test_review_attempt_preserves_classified_source_as_cause() -> None:
    """LlmCallerが返した分類済みの失敗は、そのまま原因として包まれreasonを名乗る。"""
    cause = AIProviderResultError(reason=AIProviderResultReason.RESPONSE_UNPARSEABLE)

    with pytest.raises(EvidenceReviewError) as raised:
        await _review_attempt(
            agent=EVIDENCE_REVIEWER_AGENT,
            reviewer_caller=ScriptedLlmCaller([cause]),
            review_input=EvidenceReviewInput(task_groups=(), as_of=AS_OF),
            attempt_number=1,
        )

    assert raised.value.code == "response_unparseable"
    assert raised.value.__cause__ is cause
