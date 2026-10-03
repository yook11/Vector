"""Embedding Serviceの失敗理由の契約。"""

from __future__ import annotations

import pytest

from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    EmbeddingError,
    EmbeddingFailureReason,
    EmbeddingResponseInvalidError,
)


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

    def test_has_no_retry_policy(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert not hasattr(exc, "RETRYABILITY")

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingResponseInvalidError("dimension mismatch")  # type: ignore[call-arg]


@pytest.mark.parametrize("reason", ["article_missing", None])
def test_failure_reason_rejects_untyped_values(reason):
    """自由文字列を失敗理由として受け付けない。"""
    with pytest.raises(TypeError):
        EmbeddingError(reason=reason)


@pytest.mark.parametrize(
    ("error_type", "reason"),
    [
        (EmbeddingAnalyzedArticleMissingError, EmbeddingFailureReason.ARTICLE_MISSING),
        (EmbeddingResponseInvalidError, EmbeddingFailureReason.RESPONSE_INVALID),
    ],
)
def test_service_error_carries_reason_without_retry_policy(error_type, reason):
    """Serviceの失敗理由には配送側の再試行方針を持ち込まない。"""
    error = error_type()
    assert error.reason is reason
    assert not hasattr(error, "RETRYABILITY")


def test_embedding_error_directly_inherits_exception():
    """工程例外はログ固有の基底クラスへ依存しない。"""
    assert EmbeddingError.__bases__ == (Exception,)


@pytest.mark.parametrize(
    "error",
    [
        EmbeddingResponseInvalidError(),
        EmbeddingAnalyzedArticleMissingError(),
    ],
)
def test_embedding_error_uses_standard_empty_message(error):
    """メッセージ未指定の例外文字列をcodeで補完しない。"""
    assert error.args == ()
    assert str(error) == ""
