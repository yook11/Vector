"""Ingestion — 外部ソースから記事レコードを取り込む。

ユビキタス語彙:

- ``ArticleCandidate``: fetcher 境界の正規化 VO (URL 安全性 / タイトル整形済み)。
  新 Protocol Fetcher は ``FetchedArticle`` / ``PendingHtmlFetch`` を直接 yield
  するため candidate は ``IngestionService`` の DiscoveredArticle 永続化経路で
  内部利用されるのみ。
- ``DiscoveredArticleDraft``: 永続化前のドメイン入力 VO (candidate +
  ``news_source_id``)。
- ``DiscoveredArticleEntity``: システムに記録された Entity (identity / 発見時刻)。
- ``DiscoveredArticleRepository``: Draft → save_many → Entity / URL → find_by_url。
"""

from app.collection.ingestion.domain import (
    ArticleCandidate,
    DiscoveredArticleDraft,
    DiscoveredArticleEntity,
)
from app.collection.ingestion.repository import DiscoveredArticleRepository

__all__ = [
    "ArticleCandidate",
    "DiscoveredArticleDraft",
    "DiscoveredArticleEntity",
    "DiscoveredArticleRepository",
]
