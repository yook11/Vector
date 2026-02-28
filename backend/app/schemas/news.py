from datetime import datetime

from pydantic import BaseModel, ConfigDict
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
    content: str | None = None
    content_fetched_at: datetime | None = None
    keywords: list[KeywordBrief] = []
    analysis: AnalysisResponse | None = None
    is_watched: bool = False


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
    keywords_count: int | None = None  # None = all keywords targeted
    job_id: str


class EmbedResponse(BaseModel):
    """POST /api/v1/news/embed response."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    message: str
    embedded_count: int
    skipped_count: int
    error_count: int
