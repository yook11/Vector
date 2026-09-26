"""Stage 1 (article_acquisition) の取得失敗・変換失敗例外。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError, HttpTransportError


class AcquisitionSourceInvalidError(Exception):
    """有効なソースを登録済みの取得定義へ解決できない。"""


class AcquisitionConversionDefect(StrEnum):
    """acquisition がスコープ所有する変換棄却理由 (自己記述コード)。

    value はそのまま audit の ``outcome_code`` に焼かれる (analysis BC の
    ``AnalyzableArticleDefect`` と同形)。URL 欠落は acquisition の取得不成立、
    非空 URL の不正は責任元 ``CanonicalArticleUrl`` の不変条件違反として分ける。
    後者は ``SafeUrlInvalidReason`` を直接運ぶため、ここには載らない。
    """

    URL_MISSING = "acquisition_conversion_url_missing"
    TITLE_MISSING = "acquisition_conversion_title_missing"
    UNEXPECTED_ERROR = "acquisition_conversion_unexpected_error"


@dataclass(frozen=True, slots=True)
class RssFeedFailure:
    """取得に失敗したフィードと元の例外を保持する。"""

    feed_url: str
    error: (
        HttpResponseError
        | HttpTransportError
        | HostBlockedError
        | UnreadableResponseError
    )


class RssFeedErrors(Exception):
    """フィードごとの取得失敗をまとめ、投げる条件は取得側に任せる。"""

    CODE: ClassVar[str] = "rss_feed_errors"

    def __init__(self, failures: list[RssFeedFailure]) -> None:
        super().__init__()
        if not failures:
            raise ValueError("RSS feed failures must not be empty")
        self.failures = tuple(failures)
        self.failure_count = len(self.failures)
