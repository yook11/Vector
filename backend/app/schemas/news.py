from datetime import datetime

from app.domain.news_source import SourceName
from app.models.article_analysis import ImpactLevel
from app.schemas.base import _CamelBase
from app.schemas.embeds import KeywordEmbed, OriginalArticleEmbed


class NewsBrief(_CamelBase):
    """GET /api/v1/news — 一覧カード用"""

    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    source_name: SourceName
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False


class NewsDetail(_CamelBase):
    """GET /api/v1/news/{id} — 詳細画面用"""

    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    analyzed_at: datetime
    source_name: SourceName
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False
    original: OriginalArticleEmbed


class PaginatedNewsResponse(_CamelBase):
    """Paginated list of news articles."""

    items: list[NewsBrief]
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
