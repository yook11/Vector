"""Stage 1 (article_acquisition) の marker / 変換失敗例外。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.audit.failure_projection import FailureAction, Retryability
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.external_fetch_errors import ExternalFetchError
from app.logfire.exceptions import VectorDomainError


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


class AcquisitionReadError(AcquisitionError):
    """source を read する失敗 (取得 / 読取 を集約した Stage 1 marker)。

    fetch (接続境界 ``ExternalFetchError``) と read (構造化境界
    ``UnreadableResponseError``) はどちらも「source を読めなかった」失敗で、origin が
    ``CODE`` / 型 / ``_default_message`` で既に自己記述している。marker は origin を
    そのまま hold し、段境界で要る分類だけを origin から per-instance で導く:

    - ``code`` = origin の ``CODE`` (outcome_code に焼く)。
    - ``FAILURE_KIND`` = origin 種別 (fetch=``external_fetch`` /
      read=``unreadable_response``)。
    - ``RETRYABILITY`` = read は全 terminal なので ``NON_RETRYABLE`` 固定、fetch は
      origin 自身の ``retryable`` (失敗の性質、SSoT) を ``Retryability`` enum へ変換。

    ``code`` / ``FAILURE_KIND`` / ``RETRYABILITY`` は instance 属性。projection
    (``project_marker_failure``) が大文字 ``FAILURE_KIND`` / ``RETRYABILITY`` を
    getattr し ``code`` を小文字で先読みする配線に合わせる。``__str__``
    (``SAFE_ATTRS``) は ``code`` のみ公開し origin の生 message を載せない。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    origin: ExternalFetchError | UnreadableResponseError
    code: str
    FAILURE_KIND: str  # per-instance (origin 種別から導出)
    RETRYABILITY: Retryability  # per-instance (read=terminal / fetch=origin.retryable)

    def __init__(
        self,
        *,
        origin: ExternalFetchError | UnreadableResponseError,
    ) -> None:
        super().__init__()
        self.origin = origin
        self.code = origin.CODE
        if isinstance(origin, ExternalFetchError):
            self.FAILURE_KIND = "external_fetch"
            self.RETRYABILITY = (
                Retryability.RETRYABLE
                if origin.retryable
                else Retryability.NON_RETRYABLE
            )
        else:
            self.FAILURE_KIND = "unreadable_response"
            self.RETRYABILITY = Retryability.NON_RETRYABLE


def map_origin_to_acquisition(
    exc: ExternalFetchError | UnreadableResponseError,
) -> AcquisitionReadError:
    """取得 / 読取 origin error を Stage 1 統合 marker に詰め替える。

    fetch / read の分類は ``AcquisitionReadError.__init__`` が origin から導くため、
    ここは union 型一致を検査して詰め替えるだけ (型注釈上 ``TypeError`` は不到達だが、
    動的経路の混入を弾く防御ガードとして残す)。
    """
    if isinstance(exc, ExternalFetchError | UnreadableResponseError):
        return AcquisitionReadError(origin=exc)
    raise TypeError(f"unmapped acquisition origin error: {type(exc).__qualname__}")
