"""本文抽出と記事完成に固有の失敗を伝える新経路用エラー。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.collection.domain.analyzable_article import AnalyzableArticleDefect
from app.logfire.exceptions import VectorDomainError


class ArticleCompletionError(VectorDomainError):
    """補完固有の発生事実を表し、再試行や終了は判断しない。"""

    CODE: ClassVar[str] = "article_completion_error"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)


class ArticleContentTypeError(ArticleCompletionError):
    """HTML抽出の入力として扱えなかったContent-Typeを保持する。"""

    CODE: ClassVar[str] = "article_content_type_error"

    def __init__(self, *, content_type: str | None) -> None:
        super().__init__()
        self.content_type = content_type


class ArticleExtractionEmptyError(ArticleCompletionError):
    """抽出処理は終了したが、抽出結果が得られなかった。"""

    CODE: ClassVar[str] = "article_extraction_empty"


class ArticleExtractionCrashReason(StrEnum):
    """抽出結果の欠如と区別すべき、抽出処理そのものの異常。"""

    EXCEPTION = "exception"
    UNEXPECTED_RESULT = "unexpected_result"


class ArticleExtractionCrashedError(ArticleCompletionError):
    """抽出の異常種別を保持し、元の例外は変換側が例外チェーンで伝える。"""

    CODE: ClassVar[str] = "article_extraction_crashed"

    def __init__(self, *, reason: ArticleExtractionCrashReason) -> None:
        super().__init__()
        self.reason = reason


class ArticleContentQualityError(ArticleCompletionError):
    """品質判定時の観測値を保持し、本文断片は持たない。"""

    CODE: ClassVar[str] = "article_content_quality_error"

    def __init__(self, *, body_length: int, title_present: bool) -> None:
        super().__init__()
        self.body_length = body_length
        self.title_present = title_present


class ArticleCompletionRejectedError(ArticleCompletionError):
    """記事の構築で判明した既存のdefectを保持し、品質を再判定しない。"""

    CODE: ClassVar[str] = "article_completion_rejected"

    def __init__(self, *, defects: tuple[AnalyzableArticleDefect, ...]) -> None:
        super().__init__()
        self.defects = defects
