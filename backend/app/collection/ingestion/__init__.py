"""Ingestion — 外部ソースから記事レコードを取り込む。

ユビキタス語彙:

- Protocol Fetcher は ``FetchedEntry`` envelope (item=ReadyForArticle |
  IncompleteArticle) を yield し、``IngestionService`` がそれを永続化する。
"""

__all__: list[str] = []
