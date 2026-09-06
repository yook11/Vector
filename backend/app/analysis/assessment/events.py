from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ArticleAssessedInScope(BaseModel):
    """In Scopeの分析結果を保存した場合だけ発行し、Out of Scopeは対象にしない。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.assessed_in_scope"
    SCHEMA_VERSION: ClassVar[int] = 1

    curation_id: int = Field(gt=0)
    analyzed_article_id: int = Field(gt=0)
