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


class AcquisitionConversionDefect(StrEnum):
    """acquisition がスコープ所有する変換棄却理由 (自己記述コード)。

    value はそのまま audit の ``outcome_code`` に焼かれる (analysis BC の
    ``AnalyzableArticleDefect`` と同形)。URL 不正は責任元 ``CanonicalArticleUrl``
    の ``SafeUrlInvalidReason`` を直接運ぶため、ここには載らない。本 enum は
    収集側固有の理由 (title 欠落 / precondition 通過後の想定外バグ) のみを持つ。
    """

    TITLE_MISSING = "acquisition_conversion_title_missing"
    UNEXPECTED_ERROR = "acquisition_conversion_unexpected_error"


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
