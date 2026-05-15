"""Stage 5 (Embedding) Layer 1 / Layer 2-B marker の振る舞いテスト。"""

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
        original = AIProviderRateLimitedError("rate limited")
        exc = EmbeddingRecoverableError(
            "wrapped",
            code="ai_error_rate_limited",
            provider_error=original,
        )

        assert exc.code == "ai_error_rate_limited"
        assert exc.provider_error is original
        assert str(exc) == "wrapped"

    def test_provider_error_defaults_to_none(self) -> None:
        # Layer 2-B で provider_error なしで raise するための準備。
        exc = EmbeddingRecoverableError(
            "no provider",
            code="embedding_response_invalid",
        )

        assert exc.code == "embedding_response_invalid"
        assert exc.provider_error is None

    def test_message_defaults_to_empty_string(self) -> None:
        exc = EmbeddingRecoverableError(code="x")

        assert exc.code == "x"
        assert exc.provider_error is None
        assert str(exc) == ""

    def test_code_is_keyword_only_required(self) -> None:
        # ``code`` は keyword-only かつ required (positional 渡しは reject)。
        with pytest.raises(TypeError):
            EmbeddingRecoverableError("msg")  # type: ignore[call-arg]


class TestEmbeddingTerminalSkipError:
    """``EmbeddingTerminalSkipError`` の constructor / instance attr 振る舞い。"""

    def test_holds_code_and_provider_error(self) -> None:
        original = AIProviderConfigurationError("bad api key")
        exc = EmbeddingTerminalSkipError(
            "wrapped",
            code="ai_error_configuration",
            provider_error=original,
        )

        assert exc.code == "ai_error_configuration"
        assert exc.provider_error is original
        assert str(exc) == "wrapped"

    def test_provider_error_defaults_to_none(self) -> None:
        exc = EmbeddingTerminalSkipError(
            "no provider",
            code="ai_error_input_rejected",
        )

        assert exc.code == "ai_error_input_rejected"
        assert exc.provider_error is None

    def test_code_is_keyword_only_required(self) -> None:
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
        exc = EmbeddingResponseInvalidError("dimension mismatch")
        assert exc.code == "embedding_response_invalid"

    def test_provider_error_is_none(self) -> None:
        # Stage 5 工程由来なので provider 例外起源ではない
        exc = EmbeddingResponseInvalidError("dimension mismatch")
        assert exc.provider_error is None

    def test_message_propagates(self) -> None:
        exc = EmbeddingResponseInvalidError("dimension mismatch")
        assert str(exc) == "dimension mismatch"

    def test_is_not_terminal_skip(self) -> None:
        # Layer 2-B Recoverable は TerminalSkip の subclass ではない
        assert not issubclass(EmbeddingResponseInvalidError, EmbeddingTerminalSkipError)
