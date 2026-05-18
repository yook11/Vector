"""Stage 1 (source_fetch) の marker / per-entry 変換失敗例外。

``app.collection.errors.SourceFetchError`` と同名だが別 module の別クラス。
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


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
    OBSERVED_BUILD_FAILED = "observed_build_failed"


class FetchedArticleConversionError(Exception):
    """``FetchedArticle`` を ``AnalyzableArticle`` / ``ObservedArticle`` の
    どちらにも変換できなかった失敗。

    ``message`` は決定的・非秘匿の英語文字列。秘匿値混入の可能性がある
    ``raw_url`` は素の値を保持し、redact は監査永続化側の責務。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` / ``outcome_code`` 列)。
        analyzable_reason: なぜ Analyzable にできなかったか。
        observed_reason: なぜ Observed にもできなかったか。
        source_name: 出所のソース表示名。
        raw_url: 変換前の生 URL (無い / 取れない場合 ``None``)。
        has_title: title が存在したか (trim 前で観測)。
        body_length: body 候補の長さ (無い場合 ``None``)。
        has_published_at: published_at hint が存在したか。
    """

    CODE: ClassVar[str] = "fetched_article_conversion_failed"

    code: str
    analyzable_reason: ConversionReason
    observed_reason: ConversionReason
    source_name: str | None
    raw_url: str | None
    has_title: bool
    body_length: int | None
    has_published_at: bool

    def __init__(
        self,
        message: str,
        *,
        analyzable_reason: ConversionReason,
        observed_reason: ConversionReason,
        source_name: str | None,
        raw_url: str | None,
        has_title: bool,
        body_length: int | None,
        has_published_at: bool,
    ) -> None:
        super().__init__(message)
        self.code = self.CODE
        self.analyzable_reason = analyzable_reason
        self.observed_reason = observed_reason
        self.source_name = source_name
        self.raw_url = raw_url
        self.has_title = has_title
        self.body_length = body_length
        self.has_published_at = has_published_at


class SourceFetchError(Exception):
    """ソース全体の取得に失敗したことを示す Stage 1 marker。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列)。
    """

    code: str

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
