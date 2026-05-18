"""未完成記事 (ObservedArticle) を完成形 (AnalyzableArticle) に補完する。"""

from app.collection.article_completion.completer import (
    ArticleHtmlCompleter,
    CompletionFailure,
    FetchFailed,
)
from app.collection.article_completion.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
    ExtractionEmptyReason,
)

__all__ = [
    "ArticleHtmlCompleter",
    "ArticleHtmlExtractor",
    "CompletionFailure",
    "ExtractedContent",
    "ExtractionEmpty",
    "ExtractionEmptyReason",
    "FetchFailed",
]
