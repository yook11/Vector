"""Briefing error class の監査属性テスト。"""

from __future__ import annotations

from app.audit.domain.event import Stage
from app.audit.failure_projection import Retryability
from app.insights.briefing.llm.errors import (
    BriefingConfigurationError,
    BriefingError,
    BriefingLlmError,
    BriefingResponseInvalidError,
)


def test_briefing_configuration_error_classvars_are_audit_projection_ssot() -> None:
    assert BriefingError.STAGE is Stage.BRIEFING
    assert BriefingConfigurationError.CODE == "briefing_configuration_error"
    assert BriefingConfigurationError.FAILURE_KIND == "configuration"
    assert BriefingConfigurationError.RETRYABILITY is Retryability.NON_RETRYABLE
    assert BriefingConfigurationError.FAILURE_ACTION is None


def test_briefing_llm_error_classvars_are_audit_projection_ssot() -> None:
    provider_error = RuntimeError("upstream")
    exc = BriefingLlmError(provider_error=provider_error)

    assert exc.STAGE is Stage.BRIEFING
    assert exc.CODE == "briefing_llm_error"
    assert exc.FAILURE_KIND == "llm_error"
    assert exc.RETRYABILITY is Retryability.RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.provider_error is provider_error


def test_briefing_response_invalid_error_classvars_are_audit_projection_ssot() -> None:
    exc = BriefingResponseInvalidError()

    assert exc.STAGE is Stage.BRIEFING
    assert exc.CODE == "briefing_response_invalid"
    assert exc.FAILURE_KIND == "response_invalid"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None
