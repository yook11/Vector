"""Briefing LLM 呼出の最小エラー階層。

設計方針:
- 詳細な ``_translate_error`` 多階層分類は briefing では不要
  (`feedback_failure_visibility.md`): OpenAI SDK 例外はそのまま raise し、
  taskiq の retry / failure tracking に判断を委ねる
- ``BriefingConfigurationError`` だけ用意し、API key 欠落は fail-fast
"""

from __future__ import annotations

from typing import ClassVar

from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability


class BriefingError(Exception):
    """Briefing 系処理の基底例外。"""

    STAGE: ClassVar[Stage] = Stage.BRIEFING


class BriefingConfigurationError(BriefingError):
    """設定不整合 (API key 未設定等)。retry しても解決しないため fail-fast。"""

    CODE: ClassVar[str] = "briefing_configuration_error"
    FAILURE_KIND: ClassVar[str] = "configuration"
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None
