from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.domain.keyword import KeywordName
from app.schemas.category import CategoryBrief


class KeywordCreate(BaseModel):
    """POST /api/v1/keywords request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    name: KeywordName = Field(description="Keyword tag name (1-100 chars)")
    category_id: int


class KeywordUpdate(BaseModel):
    """PATCH /api/v1/keywords/{id} request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    category_id: int | None = None


class KeywordResponse(BaseModel):
    """Keyword in API responses (list, detail)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    name: str
    category: CategoryBrief
    status: str
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
    name: str
    category: CategoryBrief
