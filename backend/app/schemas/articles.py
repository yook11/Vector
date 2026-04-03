"""Read-facing schemas for analyzed articles."""

from datetime import datetime

from app.models.article_analysis import ImpactLevel
from app.schemas.base import _CamelBase
from app.schemas.embeds import KeywordEmbed, NewsSourceEmbed, OriginalArticleEmbed


class ArticleBrief(_CamelBase):
    """GET /api/v1/articles — 一覧カード用"""

    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    source: NewsSourceEmbed
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False


class ArticleDetail(_CamelBase):
    """GET /api/v1/articles/{id} — 詳細画面用"""

    id: int
    translated_title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    analyzed_at: datetime
    source: NewsSourceEmbed
    published_at: datetime | None = None
    keywords: list[KeywordEmbed] = []
    is_watched: bool = False
    original: OriginalArticleEmbed


class PaginatedArticleResponse(_CamelBase):
    """Paginated list of articles."""

    items: list[ArticleBrief]
    total: int
    page: int
    per_page: int
    total_pages: int
