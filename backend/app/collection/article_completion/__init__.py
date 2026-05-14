"""Article Completion — Pattern H (IncompleteArticle → ReadyForArticle) の補完
責務を担うパッケージ。

ユビキタス語彙:

- ``ArticleCompletionService``: pending_html_articles 駆動の補完 use case。
- ``ArticleHtmlExtractor`` / ``ExtractedContent`` / ``ExtractionEmpty``:
  AI 境界 (HTML 抽出器) の戻り値。
- ``dispatch_html_fetch_jobs`` / ``sweep_expired_leases``: 補完 task の cron 駆動。
- ``compute_next_delay_minutes``: ``TemporaryFetchError`` 系の retry policy 純関数。

PR 4 で ``extraction/`` から rename: 「HTTP fetch する技術名」ではなく
「Pattern H の completion 責務」として命名軸を揃えた。
"""

from app.collection.article_completion.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
    ExtractionEmptyReason,
)

__all__ = [
    "ArticleHtmlExtractor",
    "ExtractedContent",
    "ExtractionEmpty",
    "ExtractionEmptyReason",
]
