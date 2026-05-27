"""Stage 1 (article_acquisition) の marker / per-entry 変換失敗例外。

``SourceAcquisitionError`` は stage1 専用の取得失敗 marker。共用の fetch transport
例外階層の基底 ``app.collection.errors.SourceFetchError`` とは別物 (あちらは I/O
動詞 ``fetch`` の語彙、本 module は工程 ``acquisition`` の語彙)。
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.audit.domain.event import Stage
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

    ``AnalyzableArticle`` 不成立は想定内の正常系 (Ready 候補 ⊆ Observed 候補)
    のため、失敗 reason は ``ObservedArticle`` にもなれなかった理由 1 つで足りる。

    ``message`` は決定的・非秘匿の英語文字列。秘匿値混入の可能性がある
    ``raw_url`` は素の値を保持し、redact は監査永続化側の責務。

    Attributes:
        code: audit event code (``outcome_code`` に焼く値)。
        conversion_reason: なぜ Observed にもなれなかったか (= 変換失敗理由)。
        source_name: 出所のソース表示名。
        raw_url: 変換前の生 URL (無い / 取れない場合 ``None``)。
        has_title: title が存在したか (trim 前で観測)。
        body_length: body 候補の長さ (無い場合 ``None``)。
        has_published_at: published_at hint が存在したか。
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
    """応答は受領したが構造化できない read 段固有の失敗 (接続エラーではない)。

    接続 (transport/status/SSRF) は成功し payload は受理したのに、RSS bozo / XML
    syntax / HTML parse / JSON decode / envelope shape 不正で構造化できないことを
    表す。接続境界の ``ExternalFetchError`` とは別系統 — 「接続できたか」ではなく
    「読めたか」の軸であり、接続エラーの SSoT (``external_fetch_errors.py``) には
    置かない。記事品質ゲートの不合格は domain validation として別軸で扱う
    (normalize が ``None``/``""`` に畳むのでこのエラーにはならない)。

    Attributes:
        CODE: audit event code (``outcome_code`` に焼く値)。接続コードと
            別カテゴリと分かるよう ``fetch_`` でなく ``read_`` prefix。
    """

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
    """ソース全体の取得に失敗したことを示す Stage 1 marker。

    Attributes:
        code: audit event code (``outcome_code`` に焼く値)。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)

    code: str

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
