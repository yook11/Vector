"""未完成記事 (ObservedArticle) を完成形 (AnalyzableArticle) に補完する。"""

from app.collection.article_completion.completer import (
    ArticleHtmlCompleter,
    CompletionFailure,
    FetchFailed,
)
from app.collection.article_completion.extraction_failure import (
    ExtractionCrashed,
    ExtractionFailure,
    NotHtml,
    ParserRejected,
    QualityGateFailed,
)
from app.collection.article_completion.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
)

__all__ = [
    "ArticleHtmlCompleter",
    "ArticleHtmlExtractor",
    "CompletionFailure",
    "ExtractedContent",
    "ExtractionCrashed",
    "ExtractionFailure",
    "FetchFailed",
    "NotHtml",
    "ParserRejected",
    "QualityGateFailed",
]
