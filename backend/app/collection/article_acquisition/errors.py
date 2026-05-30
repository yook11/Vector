"""Stage 1 (article_acquisition) の marker / 変換失敗例外。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.external_fetch_errors import ExternalFetchError
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


class AcquisitionExternalFetchError(SourceAcquisitionError):
    """Stage 1 外部取得失敗 marker。

    retry 可否は origin error 自身の ``retryable`` (失敗の性質、SSoT) から
    per-instance で導く。``code`` と同じく origin から運ぶため leaf を分けない。
    family の bool を audit の ``Retryability`` enum へ変換するのは marker の責務
    (family を audit 語彙に結合させず段境界を保つ)。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "external_fetch"
    FAILURE_ACTION: ClassVar[FailureAction | None] = None
    RETRYABILITY: Retryability  # per-instance (origin.retryable から導出)

    def __init__(
        self,
        *,
        origin_error: ExternalFetchError | UnreadableResponseError,
    ) -> None:
        super().__init__(origin_error=origin_error)
        self.RETRYABILITY = (
            Retryability.RETRYABLE
            if isinstance(origin_error, ExternalFetchError) and origin_error.retryable
            else Retryability.NON_RETRYABLE
        )


class AcquisitionUnreadableResponseError(SourceAcquisitionError):
    """取得済み payload を Stage 1 reader が構造化できなかった失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "unreadable_response"
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None


def map_origin_to_acquisition(
    exc: ExternalFetchError | UnreadableResponseError,
) -> SourceAcquisitionError:
    """取得 / 読取 origin error を Stage 1 marker に詰め替える。

    retry 可否の分類は ``AcquisitionExternalFetchError`` が origin の
    ``retryable`` から導くため、ここでは origin の種別だけで marker を選ぶ。
    """
    if isinstance(exc, UnreadableResponseError):
        return AcquisitionUnreadableResponseError(origin_error=exc)
    if isinstance(exc, ExternalFetchError):
        return AcquisitionExternalFetchError(origin_error=exc)
    raise TypeError(f"unmapped acquisition origin error: {type(exc).__qualname__}")
