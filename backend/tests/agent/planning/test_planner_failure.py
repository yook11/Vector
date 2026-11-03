"""計画の工程の失敗の型。原因から観測用のcodeを名乗り、修復の指示を持ち込まないことを確かめる(外部依存なし)。"""

from __future__ import annotations

import pytest

from app.agent.planning.failure import PlanningError
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.ai_providers.errors import (
    AIProviderTransportError,
)
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
        pytest.param(TimeoutError(), "planning_timeout", id="timeout"),
    ],
)
def test_planning_error_names_the_cause_as_code(
    cause: Exception, expected_code: str
) -> None:
    """原因を保持し、原因の種別をcodeとして名乗ってログ用のdetailsにも載せる。"""
    error = PlanningError(cause=cause)

    assert (
        error.cause is cause,
        error.__cause__ is cause,
        error.code,
        error.details,
    ) == (True, True, expected_code, {"code": expected_code})


def test_planning_error_names_each_response_defect_without_repair_hint() -> None:
    """応答の契約違反は欠陥の種類をcodeにし、修復の指示を文にもcodeにも含めない。"""
    for defect in AgentResponseDefect:
        cause = AgentResponseInvalidError(
            defect,
            repair_hint="REPAIR_HINT_MUST_NOT_ENTER_AUDIT_2a91",
        )

        error = PlanningError(cause=cause)

        assert error.code == defect.value
        assert "REPAIR_HINT_MUST_NOT_ENTER_AUDIT_2a91" not in error.code
        assert "REPAIR_HINT_MUST_NOT_ENTER_AUDIT_2a91" not in str(error)
