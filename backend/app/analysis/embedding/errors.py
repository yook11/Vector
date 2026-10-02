"""Embedding工程で確定した業務上の失敗理由。"""

from __future__ import annotations

from enum import StrEnum


class EmbeddingFailureReason(StrEnum):
    """呼び出し側の再試行方針とは独立した失敗理由。"""

    ARTICLE_MISSING = "article_missing"
    RESPONSE_INVALID = "response_invalid"


class EmbeddingError(Exception):
    """Embedding工程で確定した失敗理由を呼び出し元へ伝える。"""

    def __init__(self, *, reason: EmbeddingFailureReason) -> None:
        if not isinstance(reason, EmbeddingFailureReason):
            raise TypeError("reason must be an EmbeddingFailureReason")
        super().__init__()
        self.reason = reason

    @property
    def code(self) -> str:
        """既存の観測コードを失敗理由から導出する。"""
        if self.reason is EmbeddingFailureReason.ARTICLE_MISSING:
            return "embedding_analyzed_article_missing"
        return "embedding_response_invalid"


class EmbeddingAnalyzedArticleMissingError(EmbeddingError):
    """処理開始時またはベクトル保存時に対象の分析記事が存在しない。"""

    def __init__(self) -> None:
        super().__init__(reason=EmbeddingFailureReason.ARTICLE_MISSING)


class EmbeddingResponseInvalidError(EmbeddingError):
    """埋め込み応答がベクトルの契約を満たさない。"""

    def __init__(self) -> None:
        super().__init__(reason=EmbeddingFailureReason.RESPONSE_INVALID)
