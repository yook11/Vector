"""取得と本文補完が共有する、記事保存の確定事実。"""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class AnalyzableArticleCreated(BaseModel):
    """分析可能な記事の新規保存が確定した事実。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.analyzable_created"
    SCHEMA_VERSION: ClassVar[int] = 1

    analyzable_article_id: int = Field(gt=0)
