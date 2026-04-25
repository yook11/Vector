"""Extraction — 取得済み記事の URL から本文・公開日時を抽出するパッケージ。

ユビキタス語彙:

- ``DiscoveredArticle`` (ingestion 由来 ORM): RSS で発見された未抽出の記事。
- ``DiscoveredLookup``: ルックアップ結果 VO (id + URL + 既存 Article)。
- ``ExtractedContent`` / ``ExtractionEmpty``: AI 境界 (HTML 抽出器) の戻り値。
- ``ArticleDraft``: AI 境界を sanitize した永続化前の正規化値 (内部用)。
- ``Article``: 抽出済み記事 Entity (analysis 以降が ``id`` で扱う)。
- ``ContentFetchOutcome``: Service の戻り値 tagged union (``ContentFetchedOutcome``
  / ``AlreadyFetchedOutcome`` / ``ContentFetchSkippedOutcome``)。
"""

from app.collection.extraction.domain import Article, PublishedAt
from app.collection.extraction.extractor import (
    ArticleHtmlExtractor,
    ExtractedContent,
    ExtractionEmpty,
    ExtractionEmptyReason,
)
from app.collection.extraction.repository import (
    ArticleRepository,
    DiscoveredArticleLookupRepository,
)
from app.collection.extraction.service import (
    AlreadyFetchedOutcome,
    ContentFetchedOutcome,
    ContentFetchOutcome,
    ContentFetchService,
    ContentFetchSkippedOutcome,
    ContentFetchSkipReason,
)

__all__ = [
    "AlreadyFetchedOutcome",
    "Article",
    "ArticleHtmlExtractor",
    "ArticleRepository",
    "ContentFetchOutcome",
    "ContentFetchService",
    "ContentFetchSkipReason",
    "ContentFetchSkippedOutcome",
    "ContentFetchedOutcome",
    "DiscoveredArticleLookupRepository",
    "ExtractedContent",
    "ExtractionEmpty",
    "ExtractionEmptyReason",
    "PublishedAt",
]
