"""直接回答の工程の失敗の型。原因から観測用のcodeを名乗ることを確かめる(外部依存なし)。"""

from __future__ import annotations

import pytest

from app.agent.answering.direct_answer.contract import DirectAnswerInvalidError
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.ai_providers.errors import AIProviderTransportError
from app.http.errors import HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)


@pytest.mark.parametrize(
    ("cause", "expected_code"),
    [
        pytest.param(
            AIProviderTransportError(
                http_error=HttpTransportError(
                    failure=HttpTransportFailure(
                        HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                    )
                )
            ),
            "ai_provider_transport_error",
            id="provider",
        ),
        pytest.param(
            DirectAnswerInvalidError(), "direct_answer_blank_response", id="blank"
        ),
        pytest.param(TimeoutError(), "direct_answer_timeout", id="timeout"),
    ],
)
def test_direct_answer_error_names_the_cause_as_code(
    cause: Exception, expected_code: str
) -> None:
    """原因を保持し、原因の種別をcodeとして名乗ってログ用のdetailsにも載せる。"""
    error = DirectAnswerError(cause=cause)

    assert (
        error.cause is cause,
        error.__cause__ is cause,
        error.code,
        error.details,
    ) == (True, True, expected_code, {"code": expected_code})
