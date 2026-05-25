"""HTML 完成段で AnalyzableArticle に昇格できなかった理由を表す値。

接続/transport 失敗は ``ExternalFetchError`` family、HTML 取得段 (scrape) の
失敗は ``ScrapeFailure`` が担う。本モジュールは completion 段のうち
「観測値 + HTML 取得結果を merge して AnalyzableArticle を構築する」段で
起きる失敗だけを扱う。失敗は構築時の不変条件違反 (published_at 欠落を含む) に
集約され、domain の ``QualityTooLow`` (例外証拠) を ``CompletionRejection`` に直接
畳む (中間型を持たない)。

設計:
- ``CompletionRejection`` は Accept 軸の単一 disposition。``reason_code`` は audit
  集計 key、例外証拠は ``error_class`` / ``error_message`` を分離保持 (audit が構造化
  列へそのまま写す)。``detail`` property は log 用に両者を畳み直す。
- ``from_quality_too_low`` が domain 失敗を audit 語彙に翻訳する唯一の入口。
- ``__post_init__`` は ``error_message`` の upper-bound truncate のみ。
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
    例外証拠は ``error_class`` (raise された例外型) と ``error_message`` (Pydantic
    message) を**分離保持**する (audit が構造化列へそのまま写すため。畳んだ文字列は
    ``detail`` property で log 用に組み直す)。
    """

    reason_code: str
    error_class: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if (
            self.error_message is not None
            and len(self.error_message) > _ERROR_MESSAGE_MAX
        ):
            object.__setattr__(
                self, "error_message", self.error_message[:_ERROR_MESSAGE_MAX]
            )

    @property
    def detail(self) -> str | None:
        """log 用に ``error_class`` + ``error_message`` を畳んだ文字列。"""
        if self.error_class and self.error_message:
            return f"{self.error_class}: {self.error_message}"
        return self.error_message

    @classmethod
    def from_quality_too_low(cls, quality: QualityTooLow) -> Self:
        """domain の構築拒否 (``QualityTooLow``) を audit 語彙に翻訳する。

        ``reason_code`` は ``completion_*`` prefix の audit 集計 key として安定。
        例外証拠 (class / message) は分離したまま運ぶ。
        """
        return cls(
            reason_code="completion_invariant_rejected",
            error_class=quality.error_class,
            error_message=quality.error_message,
        )
