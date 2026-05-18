"""Stage 1 (source_fetch) の marker / per-entry 変換失敗例外。

2 系統の失敗を表す:

- ``SourceFetchError`` — ソース全体の取得失敗 (Layer 1 marker)。
  ``ingest_source`` task 層の唯一の dispatch 軸。``ArticleAcquisitionService``
  の boundary で origin ``ExternalFetchError`` を本 marker に wrap する。
  Stage 1 は taskiq inline retry を持たない (cron 一本化、``max_retries=0``)
  ため marker は 1 種のみ — Stage 2 の ``Permanent`` / ``Temporary*`` のような
  細分は持たない (原則: Stage 共通 marker は作らない、Stage 4 と同思想)。
  ``app.collection.errors.SourceFetchError`` (Stage 2 が継承軸で使用) と同名
  だが別 module の別クラス。本 marker は Stage 1 runtime のみが触れ、Stage 2
  とは runtime で衝突しない。
- ``FetchedArticleConversionError`` — 1 ``FetchedArticle`` を
  ``AnalyzableArticle`` にも ``ObservedArticle`` にも変換できなかった
  per-entry 失敗。``convert_fetched_article`` (純粋関数) が raise し、
  ``ArticleFetcher`` が stream 境界で ``ConversionRejection`` 値に変換する。
  source 全体は止めず、Service が別 tx で監査する。
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class ConversionReason(StrEnum):
    """``FetchedArticle`` 変換が不成立になった理由語彙。

    ``analyzable_reason`` / ``observed_reason`` の 2 ターゲットそれぞれに付与し、
    「なぜ Analyzable にできず、なぜ Observed にもできなかったか」を
    ``pipeline_events.payload`` の構造化列で SQL drill-down 可能にする。
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

    Analyzable / Observed 双方の不成立理由を ``ConversionReason`` で持ち、
    監査時に構造化列へ展開する。``message`` は決定的・非秘匿の英語文字列
    (例: ``"analyzable rejected: body_too_short; observed rejected: missing_title"``)。
    秘匿値混入の可能性がある ``raw_url`` は監査永続化側で ``redact_secrets``
    を通す責務 (本例外は素の値を保持する)。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` / ``outcome_code`` 列)。
            per-entry 変換不能は単一 code で集計 cardinality を抑え、細分は
            ``analyzable_reason`` / ``observed_reason`` で drill-down する。
        analyzable_reason: なぜ Analyzable にできなかったか。
        observed_reason: なぜ Observed にもできなかったか。
        source_name: 出所のソース表示名 (FK 切断耐性のための冗長保持)。
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
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            boundary で origin ``ExternalFetchError.CODE`` を引き継ぐ。
    """

    code: str

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
