"""未完成記事 (ObservedArticle) を完成形 (AnalyzableArticle) に補完する。"""

from app.collection.article_completion.acquirer import (
    AcquiredContent,
    ArticleHtmlAcquirer,
)
from app.collection.article_completion.acquisition_failure import (
    AcquisitionFailure,
    FetchFailed,
    NotHtml,
    ParseCrashed,
    ParserGaveUp,
    QualityGateFailed,
)
from app.collection.article_completion.completer import ArticleHtmlCompleter

__all__ = [
    "AcquiredContent",
    "AcquisitionFailure",
    "ArticleHtmlAcquirer",
    "ArticleHtmlCompleter",
    "FetchFailed",
    "NotHtml",
    "ParseCrashed",
    "ParserGaveUp",
    "QualityGateFailed",
]
