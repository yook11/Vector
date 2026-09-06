from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ArticleCompletedToAnalyzable(BaseModel):
    """本文補完による分析可能な記事の保存が確定した場合のイベント。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.completed_to_analyzable"
    SCHEMA_VERSION: ClassVar[int] = 1

    incomplete_article_id: int = Field(gt=0)
    analyzable_article_id: int = Field(gt=0)
