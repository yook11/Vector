"""Stage 5 (Embedding) Layer 1 / Layer 2-B marker の振る舞いテスト。

Layer 1 marker は retry 軸 (``RETRYABILITY``) だけを型で固定し、原因軸
(``failure_kind`` = 回復クラス / ``failure_reason`` = 詳細) は instance 値で持つ。
``Recoverable`` / ``Terminal`` はどちらも具象で同形の kwargs-only constructor。
hold は marker 型ではなく handler が provider mode から導出するため、旧
``*StageBlocked`` / ``*TargetRejected`` は存在しない (Stage 4 と完全同形)。
"""

from __future__ import annotations

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderRateLimitedError,
)
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalError,
)
from app.audit.failure_projection import Retryability

_LAYER1_MARKERS = (EmbeddingRecoverableError, EmbeddingTerminalError)


class TestEmbeddingRecoverableError:
    """``EmbeddingRecoverableError`` の constructor / instance attr 振る舞い。"""

    def test_holds_cause_axis_and_provider_error(self) -> None:
        original = AIProviderRateLimitedError()
        exc = EmbeddingRecoverableError(
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
        exc = EmbeddingRecoverableError(
            code="embedding_response_invalid",
            failure_kind="ai_response_invalid",
        )

        assert exc.failure_reason is None
        assert exc.provider_error is None

    def test_str_renders_code_only(self) -> None:
        exc = EmbeddingRecoverableError(
            code="ai_error_rate_limited",
            failure_kind="time_based_recovery",
            failure_reason="rate_limited",
        )
        assert str(exc) == "EmbeddingRecoverableError(code='ai_error_rate_limited')"

    def test_code_and_failure_kind_are_required(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingRecoverableError(code="x")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            EmbeddingRecoverableError(failure_kind="x")  # type: ignore[call-arg]

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingRecoverableError("msg")  # type: ignore[call-arg]


class TestEmbeddingTerminalError:
    """``EmbeddingTerminalError`` は具象 (旧 abstract / subclass 強制は撤去)。"""

    def test_is_concrete_and_holds_cause_axis(self) -> None:
        original = AIProviderConfigurationError()
        exc = EmbeddingTerminalError(
            code="ai_error_configuration",
            failure_kind="operator_action_required",
            provider_error=original,
        )

        assert exc.code == "ai_error_configuration"
        assert exc.failure_kind == "operator_action_required"
        assert exc.failure_reason is None
        assert exc.provider_error is original

    def test_str_renders_code_only(self) -> None:
        exc = EmbeddingTerminalError(
            code="ai_error_input_rejected",
            failure_kind="target_rejected",
            failure_reason="safety",
        )
        assert str(exc) == "EmbeddingTerminalError(code='ai_error_input_rejected')"

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingTerminalError("msg")  # type: ignore[call-arg]


class TestStage5MarkerHierarchy:
    """Stage 5 marker の型階層 (retry 軸 = Recoverable / Terminal の 2 本)。"""

    @pytest.mark.parametrize("marker", _LAYER1_MARKERS)
    def test_layer1_subclasses_embedding_error(
        self, marker: type[EmbeddingError]
    ) -> None:
        assert issubclass(marker, EmbeddingError)

    def test_recoverable_and_terminal_are_disjoint(self) -> None:
        assert not issubclass(EmbeddingRecoverableError, EmbeddingTerminalError)
        assert not issubclass(EmbeddingTerminalError, EmbeddingRecoverableError)

    def test_embedding_error_is_exception(self) -> None:
        assert issubclass(EmbeddingError, Exception)

    def test_marker_classvars_are_audit_projection_contract(self) -> None:
        assert not hasattr(EmbeddingError, "STAGE")
        assert EmbeddingRecoverableError.RETRYABILITY is Retryability.RETRYABLE
        assert EmbeddingTerminalError.RETRYABILITY is Retryability.NON_RETRYABLE
        assert EmbeddingRecoverableError.FAILURE_ACTION is None
        assert EmbeddingTerminalError.FAILURE_ACTION is None


class TestEmbeddingResponseInvalidError:
    """Layer 2-B marker: ``EmbeddingResponseInvalidError`` (Recoverable 系)。"""

    def test_is_recoverable_subclass(self) -> None:
        assert issubclass(EmbeddingResponseInvalidError, EmbeddingRecoverableError)

    def test_holds_fixed_code(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.code == "embedding_response_invalid"

    def test_failure_kind_is_ai_response_invalid(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.failure_kind == "ai_response_invalid"

    def test_provider_error_and_reason_are_none(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.provider_error is None
        assert exc.failure_reason is None

    def test_str_renders_code_only(self) -> None:
        exc = EmbeddingResponseInvalidError()
        expected = "EmbeddingResponseInvalidError(code='embedding_response_invalid')"
        assert str(exc) == expected

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingResponseInvalidError("dimension mismatch")  # type: ignore[call-arg]

    def test_is_not_terminal(self) -> None:
        assert not issubclass(EmbeddingResponseInvalidError, EmbeddingTerminalError)
