"""保存可能な in-scope analyzed article snapshot。"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import InScope

__all__ = ["InScopeAnalyzedArticle"]


class InScopeAnalyzedArticle(BaseModel):
    """Stage 4 in-scope 結果として保存・読み戻しできる分析済み記事 snapshot。"""

    model_config = ConfigDict(frozen=True)

    curation_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    assessment_result: InScope

    @classmethod
    def from_ready_and_assessment_result(
        cls,
        *,
        ready: ReadyForAssessment,
        assessment_result: InScope,
    ) -> Self:
        return cls(
            curation_id=ready.curation_id,
            title=ready.translated_title,
            summary=ready.summary,
            assessment_result=assessment_result,
        )

    @classmethod
    def from_persisted_values(
        cls,
        *,
        curation_id: int,
        translated_title: str,
        summary: str,
        category_slug: str,
        investor_take: str,
        key_points: object,
    ) -> Self:
        assessment_result = InScope.model_validate(
            {
                "category": category_slug,
                "investor_take": investor_take,
                "key_points": [] if key_points is None else key_points,
            }
        )
        return cls(
            curation_id=curation_id,
            title=translated_title,
            summary=summary,
            assessment_result=assessment_result,
        )
