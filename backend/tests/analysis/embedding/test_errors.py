"""Serviceの失敗理由と既存Taskiqの再試行分類の契約。"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderRateLimitedError,
)
from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    EmbeddingError,
    EmbeddingFailureReason,
    EmbeddingResponseInvalidError,
)
from app.analysis.embedding.task_errors import (
    EmbeddingRecoverableError,
    EmbeddingTaskError,
    EmbeddingTerminalError,
    to_embedding_task_error,
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
        self, marker: type[EmbeddingTaskError]
    ) -> None:
        assert issubclass(marker, EmbeddingTaskError)

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
    """応答不正はServiceの失敗理由であり、再試行分類を継承しない。"""

    def test_is_service_error_subclass(self) -> None:
        assert issubclass(EmbeddingResponseInvalidError, EmbeddingError)

    def test_holds_fixed_code(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.code == "embedding_response_invalid"

    def test_reason_is_response_invalid(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.reason is EmbeddingFailureReason.RESPONSE_INVALID

    def test_has_no_provider_error_or_retry_policy(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.provider_error is None
        assert not hasattr(exc, "RETRYABILITY")

    def test_str_renders_code_only(self) -> None:
        exc = EmbeddingResponseInvalidError()
        expected = "EmbeddingResponseInvalidError(code='embedding_response_invalid')"
        assert str(exc) == expected

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingResponseInvalidError("dimension mismatch")  # type: ignore[call-arg]

    def test_is_not_terminal(self) -> None:
        assert not issubclass(EmbeddingResponseInvalidError, EmbeddingTerminalError)


@pytest.mark.parametrize(
    ("error_type", "reason", "marker", "kind"),
    [
        (
            EmbeddingAnalyzedArticleMissingError,
            EmbeddingFailureReason.ARTICLE_MISSING,
            EmbeddingTerminalError,
            "target_missing",
        ),
        (
            EmbeddingResponseInvalidError,
            EmbeddingFailureReason.RESPONSE_INVALID,
            EmbeddingRecoverableError,
            "ai_response_invalid",
        ),
    ],
)
def test_service_reason_is_independent_of_taskiq_policy(
    error_type, reason, marker, kind
):
    """Serviceの理由をTaskiq境界だけで従来の監査・再試行分類に変換する。"""
    error = error_type()
    assert error.reason is reason
    assert not hasattr(error, "RETRYABILITY")
    assert not isinstance(error, EmbeddingTaskError)
    converted = to_embedding_task_error(error)
    assert isinstance(converted, marker)
    assert converted.code == error.code
    assert converted.failure_kind == kind
    assert converted.__cause__ is error


@pytest.mark.parametrize("reason", ["article_missing", None])
def test_failure_reason_rejects_untyped_values(reason):
    """自由文字列を失敗理由として受け付けない。"""
    with pytest.raises(TypeError):
        EmbeddingError(reason=reason)


def test_provider_reason_requires_classified_provider_error():
    """プロバイダー障害には詳細を持つ元の例外を必須とする。"""
    with pytest.raises(TypeError):
        EmbeddingError(reason=EmbeddingFailureReason.PROVIDER_ERROR)
    with pytest.raises(TypeError):
        EmbeddingError(
            reason=EmbeddingFailureReason.ARTICLE_MISSING,
            provider_error=AIProviderRateLimitedError(),
        )


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("unexpected"),
        EmbeddingRecoverableError(
            code="embedding_response_invalid", failure_kind="ai_response_invalid"
        ),
    ],
)
def test_task_adapter_preserves_non_service_errors(error):
    """想定外例外と変換済みのTaskiq例外は同じインスタンスを維持する。"""
    assert to_embedding_task_error(error) is error
