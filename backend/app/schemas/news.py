from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.analysis import AnalysisResponse
from app.schemas.keyword import KeywordBrief


class NewsResponse(BaseModel):
    """Single news article with analysis and keywords."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    title_original: str
    url: str
    source: str
    published_at: datetime | None = None
    fetched_at: datetime
    keywords: list[KeywordBrief] = []
    analysis: AnalysisResponse | None = None


class PaginatedNewsResponse(BaseModel):
    """Paginated list of news articles."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[NewsResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class NewsFetchRequest(BaseModel):
    """POST /api/v1/news/fetch request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    keyword_ids: list[int] | None = None


class NewsFetchResponse(BaseModel):
    """POST /api/v1/news/fetch response."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    message: str
    keywords_count: int
    job_id: str
