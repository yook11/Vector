"""Collection ドメイン — 外部ニュースから品質を担保した記事を獲得する BC。

サブパッケージ:

- ``article/`` — ``Article`` aggregate (``ArticleDraft`` / ``ReadyForArticle`` /
  ``Article`` + ``ArticleRepository`` + ``PublishedAt``)
- ``incomplete_article/`` — ``IncompleteArticle`` aggregate
  (``IncompleteArticle.complete_with_html()`` + ``ArticleCompletionFailed`` +
  ``PendingHtmlArticleRepository``)
- ``article_completion/`` — 補完待ち獲得記事の完成責務 (``ArticleCompletionService`` +
  ``ArticleHtmlExtractor`` + ``dispatch_html_fetch_jobs`` / ``sweep_expired_leases``
  + retry policy)
- ``fetchers/`` — 外部ソース固有の Fetcher 実装群 (Protocol)

flat モジュール:

- ``service.py`` — ``ArticleAcquisitionService`` (即時獲得 / 補完待ち獲得の
  振り分け entry point)
- ``staged.py`` — ``IngestSourceArg`` (taskiq envelope)
- ``tasks.py`` — 5 つの taskiq task (``dispatch_sources`` / ``ingest_source`` /
  ``extract_html_body``。``dispatch_html_fetch_jobs`` / ``sweep_expired_leases``
  は ``article_completion/dispatch.py`` 側で定義)
- ``errors.py`` — ``SourceFetchError`` (Stage 1 共通基底) + Stage 2 専用 subclass
  (``PermanentFetchError`` / ``TemporaryFetchError`` + 4 細分化系)
- ``url_canonicalize.py`` — URL 正規化純関数
"""
