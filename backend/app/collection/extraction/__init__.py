"""Extraction — 取得済み記事の URL から本文・公開日時を抽出するパッケージ。

ユビキタス語彙:

- ``ExtractedContent`` / ``ExtractionEmpty``: AI 境界 (HTML 抽出器) の戻り値。
- ``ArticleDraft``: AI 境界を sanitize した永続化前の正規化値 (内部用)。
- ``Article``: 抽出済み記事 Entity (analysis 以降が ``id`` で扱う)。
"""

from app.collection.extraction.domain import Article, PublishedAt
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
    ExtractionEmptyReason,
)
from app.collection.extraction.repository import ArticleRepository

__all__ = [
    "Article",
    "ArticleHtmlExtractor",
    "ArticleRepository",
    "ExtractedContent",
    "ExtractionEmpty",
    "ExtractionEmptyReason",
    "PublishedAt",
]
