"""HTML 完成段で AnalyzableArticle に昇格できなかった理由を表す値。

接続/transport 失敗は ``ExternalFetchError`` family、HTML 取得段 (scrape) の
失敗は ``ScrapeFailure`` が担う。本モジュールは completion 段のうち
「観測値 + HTML 取得結果を merge して AnalyzableArticle を構築する」段で
起きる失敗だけを扱う。失敗は構築時の不変条件違反 (published_at 欠落を含む) に
集約され、domain の ``QualityTooLow`` (例外証拠) を ``CompletionRejection`` に直接
畳む (中間型を持たない)。

設計:
- ``CompletionRejection`` は Accept 軸の単一 disposition。``reason_code`` は audit
  集計 key、``detail`` は例外証拠 (class+message) を畳んだ文字列。
- ``from_quality_too_low`` が domain 失敗を audit 語彙に翻訳する唯一の入口。
- ``__post_init__`` は ``detail`` の upper-bound truncate のみ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from app.collection.domain.analyzable_article import QualityTooLow

_ERROR_MESSAGE_MAX = 500


@dataclass(frozen=True, slots=True)
class CompletionRejection:
    """Stage 2 (完成段) のドメイン拒絶。Accept 軸の概念で Retry 軸を持たない。

    完成段の失敗は「再試行で結果が変わるか?」ではなく「ドメイン的に成立するか?」
    の判断であり、scrape concern の ``Terminal`` | ``Retryable`` とは別の型。
    pending は常に ``closed`` に閉じる (retry は発生しない)。

    ``reason_code`` は ``completion_*`` prefix の audit 集計 key として安定。
    ``detail`` は variant 固有の証拠 (例外 class+message 等) を畳んだ文字列。
    """

    reason_code: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.detail is not None and len(self.detail) > _ERROR_MESSAGE_MAX:
            object.__setattr__(self, "detail", self.detail[:_ERROR_MESSAGE_MAX])

    @classmethod
    def from_quality_too_low(cls, quality: QualityTooLow) -> Self:
        """domain の構築拒否 (``QualityTooLow``) を audit 語彙に翻訳する。

        ``reason_code`` は ``completion_*`` prefix の audit 集計 key として安定。
        例外証拠 (class+message) を ``detail`` に畳む。
        """
        return cls(
            reason_code="completion_invariant_rejected",
            detail=f"{quality.error_class}: {quality.error_message}",
        )
