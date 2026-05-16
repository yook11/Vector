"""Article Completion — Pattern H (IncompleteArticle → AnalyzableArticle) の補完
責務を担うパッケージ。

ユビキタス語彙:

- ``ArticleCompletionService``: pending_html_articles 駆動の補完 use case。
- ``ArticleHtmlExtractor`` / ``ExtractedContent`` / ``ExtractionEmpty``:
  AI 境界 (HTML 抽出器) の戻り値。
- ``ArticleHtmlCompleter`` / ``CompletionFailure`` / ``FetchFailed``:
  ``IncompleteArticle`` を ``AnalyzableArticle | CompletionFailure`` に解決する
  純粋境界 (副作用なし、fetch 例外を ``FetchFailed`` 値に畳む)。
- ``ArticleCompletionRepository``: ``pending_html_articles`` の Stage 2 操作
  (処理資格ロード / claim / sweep / 状態遷移 / 削除)。Stage 1 投入は
  ``source_fetch/pending_enqueue.py``。
- ``dispatch_html_fetch_jobs`` / ``sweep_expired_leases``: 補完 task の cron 駆動。
- ``classify_external_fetch_error`` / ``Terminal`` / ``Retryable``: Stage 2 の
  失敗分類 (``CompletionDisposition``) mapper。retry policy は ``Retryable``
  が運ぶデータ (``effective_delay_minutes`` で遅延算出)。

PR 4 で ``extraction/`` から rename: 「HTTP fetch する技術名」ではなく
「Pattern H の completion 責務」として命名軸を揃えた。
"""

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
