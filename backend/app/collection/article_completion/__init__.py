"""未完成記事 (ObservedArticle) を完成形 (AnalyzableArticle) に補完する。"""

from app.collection.article_completion.acquirer import (
    AcquiredContent,
    ArticleHtmlAcquirer,
)
from app.collection.article_completion.acquisition_failure import (
    AcquisitionFailure,
    NotHtml,
    ParseCrashed,
    ParserRejected,
    QualityGateFailed,
)
from app.collection.article_completion.completer import (
    ArticleHtmlCompleter,
    CompletionFailure,
    FetchFailed,
)

__all__ = [
    "AcquiredContent",
    "AcquisitionFailure",
    "ArticleHtmlAcquirer",
    "ArticleHtmlCompleter",
    "CompletionFailure",
    "FetchFailed",
    "NotHtml",
    "ParseCrashed",
    "ParserRejected",
    "QualityGateFailed",
]
