"""Stage 5 (Embedding) Layer 1 / Layer 2-B marker の振る舞いテスト。

Phase 4: Layer 1 marker は kwargs-only constructor、``__str__`` は SAFE_ATTRS=
("code",) のみ。Layer 2-B (``EmbeddingResponseInvalidError``) は no-arg
constructor + 固定 code。``message`` 引数経路は廃止 (PII 隔離契約)。
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
    EmbeddingTerminalStageBlockedError,
    EmbeddingTerminalTargetRejectedError,
)
from app.audit.domain.event import Stage
from app.audit.failure_projection import Retryability


class TestEmbeddingRecoverableError:
    """``EmbeddingRecoverableError`` の constructor / instance attr 振る舞い。"""

    def test_holds_code_and_provider_error(self) -> None:
        original = AIProviderRateLimitedError()
        exc = EmbeddingRecoverableError(
            code="ai_error_rate_limited",
            provider_error=original,
        )

        assert exc.code == "ai_error_rate_limited"
        assert exc.provider_error is original
        # Phase 4: __str__ は class name + code のみ (PII 隔離契約)
        assert str(exc) == "EmbeddingRecoverableError(code='ai_error_rate_limited')"

    def test_provider_error_defaults_to_none(self) -> None:
        # Layer 2-B で provider_error なしで raise するための準備。
        exc = EmbeddingRecoverableError(code="embedding_response_invalid")

        assert exc.code == "embedding_response_invalid"
        assert exc.provider_error is None

    def test_code_is_required_kwarg(self) -> None:
        # ``code`` は keyword-only かつ required。
        with pytest.raises(TypeError):
            EmbeddingRecoverableError()  # type: ignore[call-arg]

    def test_positional_message_rejected(self) -> None:
        # Phase 4: positional message 引数廃止 (PII 含有経路の構造的封鎖)。
        with pytest.raises(TypeError):
            EmbeddingRecoverableError("msg")  # type: ignore[call-arg]


class TestEmbeddingTerminalStageBlockedError:
    """``EmbeddingTerminalStageBlockedError`` の constructor / attr 振る舞い。"""

    def test_holds_code_and_provider_error(self) -> None:
        original = AIProviderConfigurationError()
        exc = EmbeddingTerminalStageBlockedError(
            code="ai_error_configuration",
            provider_error=original,
        )

        assert exc.code == "ai_error_configuration"
        assert exc.provider_error is original
        assert (
            str(exc)
            == "EmbeddingTerminalStageBlockedError(code='ai_error_configuration')"
        )

    def test_provider_error_defaults_to_none(self) -> None:
        exc = EmbeddingTerminalStageBlockedError(code="ai_error_configuration")

        assert exc.code == "ai_error_configuration"
        assert exc.provider_error is None

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingTerminalStageBlockedError("msg")  # type: ignore[call-arg]


class TestStage5MarkerHierarchy:
    """Stage 5 marker の型階層検証 (foundation marker は production から撤去済)。"""

    def test_recoverable_subclasses_embedding_error(self) -> None:
        assert issubclass(EmbeddingRecoverableError, EmbeddingError)

    def test_terminal_stage_blocked_subclasses_terminal_error(self) -> None:
        assert issubclass(EmbeddingTerminalStageBlockedError, EmbeddingTerminalError)

    def test_terminal_target_rejected_subclasses_terminal_error(self) -> None:
        assert issubclass(EmbeddingTerminalTargetRejectedError, EmbeddingTerminalError)

    def test_terminal_error_subclasses_embedding_error(self) -> None:
        assert issubclass(EmbeddingTerminalError, EmbeddingError)

    def test_terminal_error_base_is_abstract(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            EmbeddingTerminalError(code="ai_error_configuration")

    def test_terminal_subclass_must_declare_failure_kind(self) -> None:
        with pytest.raises(TypeError, match="FAILURE_KIND"):

            class _MissingFailureKind(EmbeddingTerminalError):
                pass

    def test_terminal_markers_subclass_embedding_error(self) -> None:
        assert issubclass(EmbeddingTerminalStageBlockedError, EmbeddingError)
        assert issubclass(EmbeddingTerminalTargetRejectedError, EmbeddingError)

    def test_two_markers_are_disjoint(self) -> None:
        # 2 marker の階層は独立 (片方が他方の subclass にならない)。
        assert not issubclass(
            EmbeddingRecoverableError, EmbeddingTerminalStageBlockedError
        )
        assert not issubclass(
            EmbeddingTerminalStageBlockedError, EmbeddingRecoverableError
        )
        assert not issubclass(
            EmbeddingRecoverableError, EmbeddingTerminalTargetRejectedError
        )
        assert not issubclass(
            EmbeddingTerminalTargetRejectedError, EmbeddingRecoverableError
        )

    def test_embedding_error_is_exception(self) -> None:
        assert issubclass(EmbeddingError, Exception)

    def test_marker_classvars_are_audit_projection_ssot(self) -> None:
        assert EmbeddingError.STAGE is Stage.EMBEDDING
        assert EmbeddingRecoverableError.FAILURE_KIND == "recoverable"
        assert EmbeddingRecoverableError.RETRYABILITY is Retryability.RETRYABLE
        assert EmbeddingRecoverableError.FAILURE_ACTION is None
        assert (
            EmbeddingTerminalStageBlockedError.FAILURE_KIND == "terminal_stage_blocked"
        )
        assert (
            EmbeddingTerminalStageBlockedError.RETRYABILITY
            is Retryability.NON_RETRYABLE
        )
        assert EmbeddingTerminalStageBlockedError.FAILURE_ACTION is None
        assert (
            EmbeddingTerminalTargetRejectedError.FAILURE_KIND
            == "terminal_target_rejected"
        )
        assert (
            EmbeddingTerminalTargetRejectedError.RETRYABILITY
            is Retryability.NON_RETRYABLE
        )
        assert EmbeddingTerminalTargetRejectedError.FAILURE_ACTION is None


# Layer 2-B marker (Stage 5 工程由来 / provider_error=None 固定)


class TestEmbeddingResponseInvalidError:
    """Layer 2-B marker: ``EmbeddingResponseInvalidError`` (Recoverable 系)。"""

    def test_is_recoverable_subclass(self) -> None:
        assert issubclass(EmbeddingResponseInvalidError, EmbeddingRecoverableError)

    def test_is_embedding_error_subclass(self) -> None:
        assert issubclass(EmbeddingResponseInvalidError, EmbeddingError)

    def test_holds_fixed_code(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.code == "embedding_response_invalid"

    def test_provider_error_is_none(self) -> None:
        # Stage 5 工程由来なので provider 例外起源ではない
        exc = EmbeddingResponseInvalidError()
        assert exc.provider_error is None

    def test_str_renders_code_only(self) -> None:
        exc = EmbeddingResponseInvalidError()
        # Phase 4: 旧 message 引数廃止、__str__ は code のみ
        expected = "EmbeddingResponseInvalidError(code='embedding_response_invalid')"
        assert str(exc) == expected

    def test_positional_message_rejected(self) -> None:
        # Phase 4: 旧 message 引数廃止
        with pytest.raises(TypeError):
            EmbeddingResponseInvalidError("dimension mismatch")  # type: ignore[call-arg]

    def test_is_not_terminal(self) -> None:
        # Layer 2-B Recoverable は terminal 系の subclass ではない
        assert not issubclass(EmbeddingResponseInvalidError, EmbeddingTerminalError)
