"""Evidence Review工程エラーの分類契約テスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.evidence_review.failure import (
    EvidenceReviewError,
    evidence_review_error_from,
)
from app.agent.evidence_review.preparation import EvidenceReviewInput
from app.agent.evidence_review.selection import EvidenceReviewerResponse
from app.agent.evidence_review.service import _review_attempt
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.analysis.ai_provider_errors import (
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from tests.agent.evidence_review._builders import AS_OF
from tests.agent.runtime._fakes import ScriptedAgentRuntime


@pytest.mark.parametrize("code", ["", "   "])
def test_evidence_review_error_rejects_blank_code(code: str) -> None:
    with pytest.raises(ValueError, match="code must not be blank"):
        EvidenceReviewError(code=code)


@pytest.mark.parametrize(
    ("cause", "expected_code"),
    [
        pytest.param(
            AgentResponseInvalidError(
                AgentResponseDefect.RESPONSE_NOT_JSON,
                repair_hint="SECRET_REPAIR_HINT_MUST_NOT_LEAK",
            ),
            "response_not_json",
            id="runtime-defect",
        ),
        pytest.param(
            AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT),
            "timeout",
            id="provider-reason",
        ),
        pytest.param(
            AIProviderNetworkError(),
            "ai_error_network",
            id="provider-code",
        ),
        pytest.param(
            AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
            "safety",
            id="provider-content-reason",
        ),
        pytest.param(TimeoutError("SECRET_TIMEOUT_MESSAGE"), "reviewer_timeout"),
    ],
)
def test_evidence_review_error_from_maps_source_to_safe_code(
    cause: (
        AgentResponseInvalidError
        | AIProviderNetworkError
        | AIProviderOutputBlockedError
        | TimeoutError
    ),
    expected_code: str,
) -> None:
    error = evidence_review_error_from(cause)

    assert error.code == expected_code
    assert str(error) == expected_code
    assert "SECRET" not in error.code
    assert "SECRET" not in str(error)


def test_evidence_review_error_from_maps_draft_validation_error() -> None:
    with pytest.raises(ValidationError) as raised:
        EvidenceReviewerResponse.from_raw(
            selections=[{"option_index": 0, "claim": "", "why_selected": "w"}],
            missing=[],
        )

    error = evidence_review_error_from(raised.value)

    assert error.code == "output_schema_mismatch"


@pytest.mark.asyncio
async def test_review_attempt_preserves_classified_source_as_cause() -> None:
    cause = AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_OBJECT)

    with pytest.raises(EvidenceReviewError) as raised:
        await _review_attempt(
            agent=EVIDENCE_REVIEWER_AGENT,
            reviewer_runtime=ScriptedAgentRuntime([cause]),
            review_input=EvidenceReviewInput(task_groups=(), as_of=AS_OF),
            attempt_number=1,
        )

    assert raised.value.code == "response_not_object"
    assert raised.value.__cause__ is cause
