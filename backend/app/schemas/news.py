from datetime import datetime

from app.domain.news_source import SourceName
from app.schemas.base import _CamelBase
from app.schemas.embeds import AnalysisEmbed, KeywordEmbed


class NewsResponse(_CamelBase):
    """Single news article with analysis and keywords."""

    id: int
    original_title: str
    original_url: str
    source_name: SourceName
    published_at: datetime | None = None
    created_at: datetime
    original_content: str | None = None
    keywords: list[KeywordEmbed] = []
    analysis: AnalysisEmbed | None = None
    is_watched: bool = False


class PaginatedNewsResponse(_CamelBase):
    """Paginated list of news articles."""

    items: list[NewsResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class NewsFetchRequest(_CamelBase):
    """POST /api/v1/news/fetch request body."""

    source_ids: list[int] | None = None


class NewsFetchResponse(_CamelBase):
    """POST /api/v1/news/fetch response."""

    message: str
    sources_count: int | None = None
    job_id: str


class EmbedResponse(_CamelBase):
    """POST /api/v1/news/embed response."""

    message: str
    embedded_count: int
    skipped_count: int
    error_count: int
