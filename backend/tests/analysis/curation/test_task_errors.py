"""Stage 3 (Curation) Layer 1 / Layer 2-B marker の振る舞いテスト。

Layer 1 marker は retry 軸 (``RETRYABILITY``) と業務副作用 (``FAILURE_ACTION``) だけを
型で固定し、原因軸 (``failure_kind`` = 回復クラス / ``failure_reason`` = 詳細) は
instance 値で持つ。marker が 3 本あるのは TerminalDrop が記事削除
(``FAILURE_ACTION=DROP_ARTICLE``) という業務 disposition を担うため。
``__str__`` は SAFE_ATTRS=("code",) のみ (``failure_reason`` は forensic で非露出)。
"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderInputRejectedError,
    AIProviderRateLimitedError,
)
from app.ai_providers.gemini.error_translator import GeminiContentRejectionReason
from app.analysis.curation.task_errors import (
    CurationRecoverableError,
    CurationTaskError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
)
from app.audit.failure_projection import FailureAction, Retryability

# Layer 1 の 3 marker は同形 (retry / DROP 軸だけ classvar、原因軸は instance 値)。
_LAYER1_MARKERS = (
    CurationRecoverableError,
    CurationTerminalKeepError,
    CurationTerminalDropError,
)


class TestCurationRecoverableError:
    """``CurationRecoverableError`` の constructor / instance attr 振る舞い。"""

    def test_holds_cause_axis_and_provider_error(self) -> None:
        original = AIProviderRateLimitedError()
        exc = CurationRecoverableError(
            code="ai_error_rate_limited",
            failure_kind="time_based_recovery",
            failure_reason="rate_limited",
            provider_error=original,
        )

        assert exc.code == "ai_error_rate_limited"
        assert exc.failure_kind == "time_based_recovery"
        assert exc.failure_reason == "rate_limited"
        assert exc.provider_error is original

    def test_optional_attrs_default(self) -> None:
        exc = CurationRecoverableError(
            code="extraction_response_invalid",
            failure_kind="ai_response_invalid",
        )

        assert exc.failure_reason is None
        assert exc.provider_error is None

    def test_str_renders_code_only(self) -> None:
        # SAFE_ATTRS=("code",): failure_kind / failure_reason は span に載せない。
        exc = CurationRecoverableError(
            code="ai_error_rate_limited",
            failure_kind="time_based_recovery",
            failure_reason="rate_limited",
        )
        assert str(exc) == "CurationRecoverableError(code='ai_error_rate_limited')"

    def test_code_and_failure_kind_are_required(self) -> None:
        with pytest.raises(TypeError):
            CurationRecoverableError(code="x")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            CurationRecoverableError(failure_kind="x")  # type: ignore[call-arg]

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            CurationRecoverableError("msg")  # type: ignore[call-arg]


class TestCurationTerminalDropError:
    """``CurationTerminalDropError`` は記事削除副作用を型で固定する 3 本目の marker。"""

    def test_holds_cause_axis_and_provider_error(self) -> None:
        original = AIProviderInputRejectedError(
            reason=GeminiContentRejectionReason.INPUT_BLOCKED
        )
        exc = CurationTerminalDropError(
            code="ai_error_input_rejected",
            failure_kind="target_rejected",
            failure_reason="input_blocked",
            provider_error=original,
        )

        assert exc.code == "ai_error_input_rejected"
        assert exc.failure_kind == "target_rejected"
        assert exc.failure_reason == "input_blocked"
        assert exc.provider_error is original

    def test_str_renders_code_only(self) -> None:
        exc = CurationTerminalDropError(
            code="ai_error_input_rejected",
            failure_kind="target_rejected",
            failure_reason="input_blocked",
        )
        assert str(exc) == "CurationTerminalDropError(code='ai_error_input_rejected')"


class TestStage3MarkerHierarchy:
    """Stage 3 marker の型階層 (retry / DROP 軸 = 3 本)。"""

    @pytest.mark.parametrize("marker", _LAYER1_MARKERS)
    def test_layer1_subclasses_curation_error(
        self, marker: type[CurationTaskError]
    ) -> None:
        assert issubclass(marker, CurationTaskError)

    def test_curation_error_is_exception(self) -> None:
        assert issubclass(CurationTaskError, Exception)

    def test_marker_classvars_are_audit_projection_contract(self) -> None:
        # retry / DROP 軸だけ型で固定 (原因軸 failure_kind は instance 値で別途検証)。
        assert not hasattr(CurationTaskError, "STAGE")
        assert CurationRecoverableError.RETRYABILITY is Retryability.RETRYABLE
        assert CurationRecoverableError.FAILURE_ACTION is None
        assert CurationTerminalKeepError.RETRYABILITY is Retryability.NON_RETRYABLE
        assert CurationTerminalKeepError.FAILURE_ACTION is None
        assert CurationTerminalDropError.RETRYABILITY is Retryability.NON_RETRYABLE
        assert CurationTerminalDropError.FAILURE_ACTION is FailureAction.DROP_ARTICLE
