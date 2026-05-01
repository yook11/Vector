from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction
from app.models.article_extraction_entity import ArticleExtractionEntity
from app.models.article_rejection import ArticleRejection
from app.models.auth_ref import auth_user_ref  # noqa: F401
from app.models.category import Category
from app.models.discovered_article import DiscoveredArticle
from app.models.fetch_log import FetchLog
from app.models.news_source import NewsSource, SourceType
from app.models.watchlist_entry import WatchlistEntry
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot

__all__ = [
    "Article",
    "ArticleAnalysis",
    "ArticleExtraction",
    "ArticleExtractionEntity",
    "ArticleRejection",
    "Category",
    "DiscoveredArticle",
    "FetchLog",
    "NewsSource",
    "SourceType",
    "WatchlistEntry",
    "WeeklyTrendsSnapshot",
]
