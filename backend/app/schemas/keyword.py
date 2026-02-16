from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class KeywordCreate(BaseModel):
    """POST /api/v1/keywords request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    keyword: str
    category: str = "custom"


class KeywordUpdate(BaseModel):
    """PATCH /api/v1/keywords/{id} request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    is_active: bool | None = None


class KeywordResponse(BaseModel):
    """Keyword in API responses (list, detail, embedded in news)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    keyword: str
    category: str
    is_active: bool
    article_count: int = 0
    created_at: datetime


class KeywordBrief(BaseModel):
    """Minimal keyword info embedded in NewsResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    keyword: str
    category: str
