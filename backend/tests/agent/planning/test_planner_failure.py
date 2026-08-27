"""Question planner failure classification tests."""

from __future__ import annotations

import pytest

from app.agent.planning.failure import PlanningError, planning_error_from
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.analysis.ai_provider_errors import AIProviderNetworkError


def test_planning_error_from_maps_provider_code() -> None:
    cause = AIProviderNetworkError()

    error = planning_error_from(cause)

    assert error.code == "ai_error_network"
    assert str(error) == "ai_error_network"


def test_planning_error_from_maps_each_response_defect() -> None:
    for defect in AgentResponseDefect:
        cause = AgentResponseInvalidError(
            defect,
            repair_hint="REPAIR_HINT_MUST_NOT_ENTER_AUDIT_2a91",
        )

        error = planning_error_from(cause)

        assert error.code == defect.value
        assert "REPAIR_HINT_MUST_NOT_ENTER_AUDIT_2a91" not in error.code
        assert "REPAIR_HINT_MUST_NOT_ENTER_AUDIT_2a91" not in str(error)


def test_planning_error_rejects_empty_code() -> None:
    with pytest.raises(ValueError, match="code must not be empty"):
        PlanningError(code="")
