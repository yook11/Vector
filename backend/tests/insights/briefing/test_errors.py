"""Briefing error class の監査属性テスト。"""

from __future__ import annotations

from app.audit.domain.event import Stage
from app.audit.failure_projection import Retryability
from app.insights.briefing.llm.errors import BriefingConfigurationError, BriefingError


def test_briefing_configuration_error_classvars_are_audit_projection_ssot() -> None:
    assert BriefingError.STAGE is Stage.BRIEFING
    assert BriefingConfigurationError.CODE == "briefing_configuration_error"
    assert BriefingConfigurationError.FAILURE_KIND == "configuration"
    assert BriefingConfigurationError.RETRYABILITY is Retryability.NON_RETRYABLE
    assert BriefingConfigurationError.FAILURE_ACTION is None
