from datetime import datetime

from pydantic import Field

from app.domain.keyword import KeywordName
from app.models.keyword import KeywordStatus
from app.schemas.base import _CamelBase
from app.schemas.embeds import CategoryEmbed


class KeywordCreate(_CamelBase):
    """POST /api/v1/keywords request body."""

    name: KeywordName = Field(description="Keyword tag name (1-100 chars)")
    category_id: int


class KeywordUpdate(_CamelBase):
    """PATCH /api/v1/keywords/{id} request body."""

    category_id: int | None = None


class KeywordResponse(_CamelBase):
    """Keyword in API responses (list, detail)."""

    id: int
    name: KeywordName
    category: CategoryEmbed
    status: KeywordStatus
    article_count: int = 0
    created_at: datetime


class KeywordListResponse(_CamelBase):
    """GET /api/v1/keywords response wrapper."""

    items: list[KeywordResponse]
