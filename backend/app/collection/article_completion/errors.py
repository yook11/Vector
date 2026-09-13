"""本文抽出と記事完成に固有の失敗を伝える新経路用エラー。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.collection.domain.analyzable_article import AnalyzableArticleDefect


class ArticleCompletionError(Exception):
    """補完固有の発生事実を表し、再試行や終了は判断しない。"""

    CODE: ClassVar[str] = "article_completion_error"


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
    """記事の構築拒否の理由と未分類の詳細を保持し、品質を再判定しない。"""

    CODE: ClassVar[str] = "article_completion_rejected"

    def __init__(
        self,
        *,
        defects: tuple[AnalyzableArticleDefect, ...],
        unmapped: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.defects = defects
        self.unmapped = unmapped


class FetchResource(StrEnum):
    """制限に達したときに取得していたリソース。"""

    ROBOTS_TXT = "robots_txt"
    ARTICLE_PAGE = "article_page"


class ResponseSizeBasis(StrEnum):
    """上限超過を確認したサイズの根拠。"""

    DECLARED_CONTENT_LENGTH = "declared_content_length"
    RECEIVED_DECODED_BODY = "received_decoded_body"


class RobotsDisallowedError(ArticleCompletionError):
    """robotsのルールで対象記事の取得が明示的に禁止された。"""

    CODE: ClassVar[str] = "article_robots_disallowed"


class ResponseSizeLimitExceededError(ArticleCompletionError):
    """応答サイズの上限と、超過を確認した時点の事実を保持する。"""

    CODE: ClassVar[str] = "article_response_size_limit_exceeded"

    def __init__(
        self,
        *,
        resource: FetchResource,
        limit_bytes: int,
        observed_bytes: int,
        size_basis: ResponseSizeBasis,
    ) -> None:
        super().__init__()
        self.resource = resource
        self.limit_bytes = limit_bytes
        self.observed_bytes = observed_bytes
        self.size_basis = size_basis


class FetchDeadlineExceededError(ArticleCompletionError):
    """通信の停滞とは別に、取得全体の制限時間を超えた事実を保持する。"""

    CODE: ClassVar[str] = "article_fetch_deadline_exceeded"

    def __init__(self, *, resource: FetchResource, limit_seconds: float) -> None:
        super().__init__()
        self.resource = resource
        self.limit_seconds = limit_seconds
