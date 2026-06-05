"""Stage 3 (Curation) marker の監査属性テスト。"""

from __future__ import annotations

from app.analysis.ai_provider_errors import AIProviderInputRejectedError
from app.analysis.curation.errors import (
    CurationError,
    CurationRecoverableError,
    CurationResponseInvalidError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability


def test_marker_classvars_are_audit_projection_ssot() -> None:
    assert CurationError.STAGE is Stage.CURATION
    assert CurationRecoverableError.FAILURE_KIND == "recoverable"
    assert CurationRecoverableError.RETRYABILITY is Retryability.RETRYABLE
    assert CurationRecoverableError.FAILURE_ACTION is None
    assert CurationTerminalKeepError.FAILURE_KIND == "terminal_keep"
    assert CurationTerminalKeepError.RETRYABILITY is Retryability.NON_RETRYABLE
    assert CurationTerminalKeepError.FAILURE_ACTION is None
    assert CurationTerminalDropError.FAILURE_KIND == "terminal_drop"
    assert CurationTerminalDropError.RETRYABILITY is Retryability.NON_RETRYABLE
    assert CurationTerminalDropError.FAILURE_ACTION is FailureAction.DROP_ARTICLE


def test_terminal_drop_holds_code_and_provider_error() -> None:
    original = AIProviderInputRejectedError(
        reason=GeminiContentRejectionReason.INPUT_BLOCKED
    )
    exc = CurationTerminalDropError(
        code="ai_error_input_rejected",
        provider_error=original,
    )

    assert exc.code == "ai_error_input_rejected"
    assert exc.provider_error is original


def test_layer_2b_response_invalid_keeps_existing_code_contract() -> None:
    exc = CurationResponseInvalidError()

    assert exc.code == "extraction_response_invalid"
    assert exc.provider_error is None
    assert exc.FAILURE_KIND == "recoverable"
    assert exc.RETRYABILITY is Retryability.RETRYABLE
