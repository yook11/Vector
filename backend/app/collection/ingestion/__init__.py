"""Ingestion — 外部ソースから記事レコードを取り込む。

ユビキタス語彙:

- ``ArticleCandidate``: fetcher 境界の正規化 VO (URL 安全性 / タイトル整形済み)。
  Protocol Fetcher は ``FetchedEntry`` envelope (item=ReadyForArticle |
  IncompleteArticle) を yield し、candidate は ``IngestionService`` 内部で
  正規化用途に利用される。
"""

from app.collection.ingestion.domain import ArticleCandidate

__all__ = [
    "ArticleCandidate",
]
