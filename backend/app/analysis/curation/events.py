from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ArticleCuratedSignal(BaseModel):
    """Signalの整形結果を保存した場合だけ発行し、Noiseは対象にしない。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.curated_signal"
    SCHEMA_VERSION: ClassVar[int] = 1

    analyzable_article_id: int = Field(gt=0)
    curation_id: int = Field(gt=0)
