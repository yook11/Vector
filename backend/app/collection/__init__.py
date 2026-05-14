"""Collection ドメイン — ニュース記事の取得 + Pattern H 補完を担う。

サブパッケージ:

- ``article/`` — ``Article`` aggregate (``ArticleDraft`` / ``ReadyForArticle`` /
  ``Article`` + ``ArticleRepository`` + ``PublishedAt``)
- ``incomplete_article/`` — ``IncompleteArticle`` aggregate
  (``IncompleteArticle.complete_with_html()`` + ``ArticleCompletionFailed`` +
  ``PendingHtmlArticleRepository``)
- ``article_completion/`` — Pattern H 補完責務 (``ArticleCompletionService`` +
  ``ArticleHtmlExtractor`` + ``dispatch_html_fetch_jobs`` / ``sweep_expired_leases``
  + retry policy)
- ``fetchers/`` — 外部ソース固有の Fetcher 実装群 (Protocol)

flat モジュール:

- ``service.py`` — ``IngestionService`` (Pattern R / Pattern H 振り分け entry point)
- ``staged.py`` — ``IngestSourceArg`` (taskiq envelope)
- ``tasks.py`` — 5 つの taskiq task (``dispatch_sources`` / ``ingest_source`` /
  ``extract_html_body``。``dispatch_html_fetch_jobs`` / ``sweep_expired_leases``
  は ``article_completion/dispatch.py`` 側で定義)
- ``errors.py`` — ``PermanentFetchError`` / ``TemporaryFetchError``
- ``url_canonicalize.py`` — URL 正規化純関数
"""
