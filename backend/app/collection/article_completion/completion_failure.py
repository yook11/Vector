"""complete concern で AnalyzableArticle に昇格できなかった理由を表す値。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from app.collection.domain.analyzable_article import (
    AnalyzableArticleDefect,
    QualityTooLow,
)


@dataclass(frozen=True, slots=True)
class CompletionRejection:
    """complete concern のドメイン拒絶。retry せず pending を閉じる。

    ドメインが分類した defect 集合をそのまま運び、audit に焼く
    (主 defect = outcome_code、全集合 = payload.defects)。
    """

    defects: tuple[AnalyzableArticleDefect, ...]
    unmapped: tuple[str, ...] = ()

    @property
    def reason_code(self) -> str:
        """主 defect value = audit outcome_code。"""
        return self.defects[0].value

    @property
    def defect_codes(self) -> tuple[str, ...]:
        return tuple(d.value for d in self.defects)

    @classmethod
    def from_quality_too_low(cls, quality: QualityTooLow) -> Self:
        """domain の構築拒否を completion rejection に翻訳する。"""
        return cls(defects=quality.defects, unmapped=quality.unmapped)
