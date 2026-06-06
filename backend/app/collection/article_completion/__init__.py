"""未完成記事 (ObservedArticle) を完成形 (AnalyzableArticle) に補完する。"""

from app.collection.article_completion.completer import ArticleHtmlCompleter
from app.collection.article_completion.scrape_failure import (
    ScrapeContentQualityTooLow,
    ScrapeFailure,
    ScrapeNotHtml,
    ScrapeParseCrashed,
    ScrapeParserGaveUp,
)
from app.collection.article_completion.scraper import (
    ArticleScraper,
    ScrapedContent,
)

__all__ = [
    "ArticleHtmlCompleter",
    "ArticleScraper",
    "ScrapedContent",
    "ScrapeFailure",
    "ScrapeNotHtml",
    "ScrapeParseCrashed",
    "ScrapeParserGaveUp",
    "ScrapeContentQualityTooLow",
]
