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
    EmbeddingTerminalSkipError,
)


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


class TestEmbeddingTerminalSkipError:
    """``EmbeddingTerminalSkipError`` の constructor / instance attr 振る舞い。"""

    def test_holds_code_and_provider_error(self) -> None:
        original = AIProviderConfigurationError()
        exc = EmbeddingTerminalSkipError(
            code="ai_error_configuration",
            provider_error=original,
        )

        assert exc.code == "ai_error_configuration"
        assert exc.provider_error is original
        assert str(exc) == "EmbeddingTerminalSkipError(code='ai_error_configuration')"

    def test_provider_error_defaults_to_none(self) -> None:
        exc = EmbeddingTerminalSkipError(code="ai_error_input_rejected")

        assert exc.code == "ai_error_input_rejected"
        assert exc.provider_error is None

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingTerminalSkipError("msg")  # type: ignore[call-arg]


class TestStage5MarkerHierarchy:
    """Stage 5 marker の型階層検証 (foundation marker は production から撤去済)。"""

    def test_recoverable_subclasses_embedding_error(self) -> None:
        assert issubclass(EmbeddingRecoverableError, EmbeddingError)

    def test_terminal_skip_subclasses_embedding_error(self) -> None:
        assert issubclass(EmbeddingTerminalSkipError, EmbeddingError)

    def test_two_markers_are_disjoint(self) -> None:
        # 2 marker の階層は独立 (片方が他方の subclass にならない)。
        assert not issubclass(EmbeddingRecoverableError, EmbeddingTerminalSkipError)
        assert not issubclass(EmbeddingTerminalSkipError, EmbeddingRecoverableError)

    def test_embedding_error_is_exception(self) -> None:
        assert issubclass(EmbeddingError, Exception)


# ---------------------------------------------------------------------------
# Layer 2-B marker (Stage 5 工程由来 / provider_error=None 固定)
# ---------------------------------------------------------------------------


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

    def test_is_not_terminal_skip(self) -> None:
        # Layer 2-B Recoverable は TerminalSkip の subclass ではない
        assert not issubclass(EmbeddingResponseInvalidError, EmbeddingTerminalSkipError)
