from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ArticleAcquired(BaseModel):
    """分析可能な記事の保存が確定し、整形へ進める場合のイベント。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.acquired"
    SCHEMA_VERSION: ClassVar[int] = 1

    source_id: int = Field(gt=0)
    analyzable_article_id: int = Field(gt=0)


class IncompleteArticleRecorded(BaseModel):
    """本文不足の記事の保存が確定し、本文補完へ進める場合のイベント。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.incomplete_recorded"
    SCHEMA_VERSION: ClassVar[int] = 1

    source_id: int = Field(gt=0)
    incomplete_article_id: int = Field(gt=0)
