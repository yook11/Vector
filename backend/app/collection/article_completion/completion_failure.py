"""complete concern で AnalyzableArticle に昇格できなかった理由を表す値。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from app.collection.domain.analyzable_article import QualityTooLow

_ERROR_MESSAGE_MAX = 500


@dataclass(frozen=True, slots=True)
class CompletionRejection:
    """complete concern のドメイン拒絶。retry せず pending を閉じる。"""

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
        """domain の構築拒否を completion rejection に翻訳する。"""
        return cls(
            reason_code="completion_invariant_rejected",
            error_class=quality.error_class,
            error_message=quality.error_message,
        )
