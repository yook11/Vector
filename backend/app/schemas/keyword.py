from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.keyword_category import KeywordCategoryBrief


class KeywordCreate(BaseModel):
    """POST /api/v1/keywords request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    keyword: str
    category_ids: list[int] = []


class KeywordUpdate(BaseModel):
    """PATCH /api/v1/keywords/{id} request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    category_ids: list[int] | None = None


class KeywordResponse(BaseModel):
    """Keyword in API responses (list, detail, embedded in news)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    keyword: str
    categories: list[KeywordCategoryBrief] = []
    article_count: int = 0
    created_at: datetime


class KeywordListResponse(BaseModel):
    """GET /api/v1/keywords response wrapper."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[KeywordResponse]


class KeywordBrief(BaseModel):
    """Minimal keyword info embedded in NewsResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    keyword: str
    categories: list[KeywordCategoryBrief] = []
