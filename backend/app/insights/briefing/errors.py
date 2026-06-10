"""Briefing LLM 呼出の stage marker 例外。"""

from __future__ import annotations

from typing import ClassVar

from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability


class BriefingError(Exception):
    """Briefing 系処理の基底例外。"""

    STAGE: ClassVar[Stage] = Stage.BRIEFING


class BriefingConfigurationError(BriefingError):
    """設定不整合 (API key 未設定等)。retry しても解決しないため fail-fast。"""

    CODE: ClassVar[str] = "briefing_generation_llm_configuration_invalid"
    FAILURE_KIND: ClassVar[str] = "configuration"
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None


class BriefingLlmError(BriefingError):
    """LLM provider 呼出由来の一時失敗。"""

    CODE: ClassVar[str] = "briefing_generation_llm_provider_call_failed"
    FAILURE_KIND: ClassVar[str] = "llm_error"
    RETRYABILITY: ClassVar[Retryability] = Retryability.RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    provider_error: BaseException

    def __init__(self, *, provider_error: BaseException) -> None:
        super().__init__(str(provider_error) or self.CODE)
        self.provider_error = provider_error


class BriefingLlmResponseInvalidError(BriefingError):
    """LLM 応答が briefing schema / article id 制約に合致しない。"""

    CODE: ClassVar[str] = "briefing_generation_llm_response_contract_invalid"
    FAILURE_KIND: ClassVar[str] = "response_invalid"
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    def __init__(self) -> None:
        super().__init__(self.CODE)
