"""申し送りの整理の工程の失敗の型。原因から観測用のcodeを名乗ることを確かめる(外部依存なし)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.research_handoff.failure import ResearchHandoffError
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.ai_providers.errors import (
    AIProviderError,
    AIProviderResponseError,
    AIProviderResponseReason,
)
from app.http.errors import HttpResponseError

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("cause", "expected_code"),
    [
        pytest.param(
            AgentResponseInvalidError(
                AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH,
                repair_hint="SECRET_REPAIR_HINT_MUST_NOT_LEAK",
            ),
            "output_schema_mismatch",
            id="response-defect",
        ),
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.AUTH,
                http_error=HttpResponseError(status_code=401, received_at=_RECEIVED_AT),
            ),
            "ai_provider_response_error",
            id="provider",
        ),
    ],
)
def test_research_handoff_error_names_the_cause_as_code(
    cause: AIProviderError | AgentResponseInvalidError, expected_code: str
) -> None:
    """原因を保持し、原因の種別をcodeとして名乗ってログ用のdetailsにも載せる。"""
    error = ResearchHandoffError(cause=cause)

    assert (
        error.cause is cause,
        error.__cause__ is cause,
        error.code,
        error.details,
    ) == (True, True, expected_code, {"code": expected_code})
    assert "SECRET" not in str(error)
