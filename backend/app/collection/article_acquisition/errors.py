"""Stage 1 (article_acquisition) の marker / 変換失敗例外。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchContentTypeMismatchError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRateLimitedError,
    FetchRedirectBlockedError,
    FetchRedirectLoopError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchResponseTooLargeError,
    FetchRetryableStatusError,
    FetchRobotsDisallowedError,
    FetchRobotsUnavailableError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedStatusError,
)
from app.logfire_exceptions import VectorDomainError


class ConversionReason(StrEnum):
    """``FetchedArticle`` 変換が不成立になった理由語彙。

    値は audit/監視で集計 key になるため安定な snake_case 文字列。
    """

    MISSING_TITLE = "missing_title"
    MISSING_URL = "missing_url"
    INVALID_URL = "invalid_url"
    BODY_TOO_SHORT = "body_too_short"
    BODY_TOO_LONG = "body_too_long"
    BODY_ABSENT = "body_absent"
    PUBLISHED_ABSENT = "published_absent"
    READY_PRECLUDED = "ready_precluded"
    ANALYZABLE_INVARIANT = "analyzable_invariant"
    UNEXPECTED_ERROR = "unexpected_error"


class FetchedArticleConversionError(Exception):
    """``FetchedArticle`` を ``AnalyzableArticle`` / ``ObservedArticle`` の
    どちらにも変換できなかった失敗。

    ``raw_url`` は素の値を保持し、redact は監査永続化側で行う。

    Attributes:
        code: ``outcome_code`` に焼く event code。
        conversion_reason: Observed にもなれなかった理由。
        source_name: source 表示名。
        raw_url: 変換前の URL。
        has_title: trim 前 title の有無。
        body_length: body 候補の長さ。
        has_published_at: published_at hint の有無。
    """

    CODE: ClassVar[str] = "article_conversion_rejected"

    code: str
    conversion_reason: ConversionReason
    source_name: str | None
    raw_url: str | None
    has_title: bool
    body_length: int | None
    has_published_at: bool

    def __init__(
        self,
        message: str,
        *,
        conversion_reason: ConversionReason,
        source_name: str | None,
        raw_url: str | None,
        has_title: bool,
        body_length: int | None,
        has_published_at: bool,
    ) -> None:
        super().__init__(message)
        self.code = self.CODE
        self.conversion_reason = conversion_reason
        self.source_name = source_name
        self.raw_url = raw_url
        self.has_title = has_title
        self.body_length = body_length
        self.has_published_at = has_published_at


class UnreadableResponseError(Exception):
    """応答を受け取ったが reader が構造化できなかった read-domain origin error。"""

    CODE: ClassVar[str] = "read_unreadable_response"

    def __str__(self) -> str:
        explicit = super().__str__()
        return explicit if explicit else self.CODE


class AcquisitionError(VectorDomainError):
    """Stage 1 固有例外の共通基底。

    外部接続境界の ``ExternalFetchError`` family は origin error なので、本基底を
    継承しない。Stage 1 の処理方針を持つ marker だけがここに属する。
    """

    STAGE: ClassVar[Stage] = Stage.ACQUISITION


class SourceAcquisitionError(AcquisitionError):
    """ソース全体の取得失敗を示す Stage 1 marker base。

    leaf class が retry 方針と failure kind を持つ。``code`` は origin error の
    ``CODE`` を ``outcome_code`` に焼くための instance 属性。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)

    code: str
    origin_error: ExternalFetchError | UnreadableResponseError

    def __init__(
        self,
        *,
        origin_error: ExternalFetchError | UnreadableResponseError,
    ) -> None:
        super().__init__()
        self.origin_error = origin_error
        self.code = origin_error.CODE


class AcquisitionExternalFetchRecoverableError(SourceAcquisitionError):
    """再実行で回復しうる Stage 1 外部取得失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "external_fetch"
    RETRYABILITY: ClassVar[Retryability] = Retryability.RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None


class AcquisitionExternalFetchTerminalError(SourceAcquisitionError):
    """再実行しても同じ結果になる Stage 1 外部取得失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "external_fetch"
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None


class AcquisitionUnreadableResponseError(SourceAcquisitionError):
    """取得済み payload を Stage 1 reader が構造化できなかった失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "unreadable_response"
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None


ACQUISITION_RECOVERABLE_FETCH_ERRORS: tuple[type[ExternalFetchError], ...] = (
    FetchTimeoutError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchGatewayError,
    FetchRequestTimeoutError,
    FetchRateLimitedError,
    FetchRetryableStatusError,
    FetchUnexpectedStatusError,
)
"""Stage 1 で再実行により回復しうる外部取得 origin error。"""


ACQUISITION_TERMINAL_FETCH_ERRORS: tuple[type[ExternalFetchError], ...] = (
    FetchAccessDeniedError,
    FetchLegalBlockError,
    FetchResourceNotFoundError,
    FetchSsrfBlockedError,
    FetchRobotsDisallowedError,
    FetchRobotsUnavailableError,
    FetchRedirectBlockedError,
    FetchRedirectLoopError,
    FetchResponseTooLargeError,
    FetchContentTypeMismatchError,
)
"""Stage 1 で再実行しても同じ結果になる外部取得 origin error。"""


def map_origin_to_acquisition(
    exc: ExternalFetchError | UnreadableResponseError,
) -> SourceAcquisitionError:
    """取得 / 読取 origin error を Stage 1 marker に詰め替える。"""
    if isinstance(exc, UnreadableResponseError):
        return AcquisitionUnreadableResponseError(origin_error=exc)
    if isinstance(exc, ACQUISITION_RECOVERABLE_FETCH_ERRORS):
        return AcquisitionExternalFetchRecoverableError(origin_error=exc)
    if isinstance(exc, ACQUISITION_TERMINAL_FETCH_ERRORS):
        return AcquisitionExternalFetchTerminalError(origin_error=exc)
    raise TypeError(f"unmapped acquisition origin error: {type(exc).__qualname__}")
