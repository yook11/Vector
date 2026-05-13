"""Extraction — 取得済み記事の URL から本文・公開日時を抽出するパッケージ。

ユビキタス語彙:

- ``ExtractedContent`` / ``ExtractionEmpty``: AI 境界 (HTML 抽出器) の戻り値。

PR 3 で aggregate 軸再配置を実施: ``ArticleDraft`` / ``Article`` / ``PublishedAt``
/ ``ArticleRepository`` は ``app.collection.article`` 配下に移管済。本パッケージは
extractor の API のみ公開する。
"""

from app.collection.extraction.extractor import (
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
