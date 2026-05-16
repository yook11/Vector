"""Collection ドメイン — 外部ニュースから品質を担保した記事を獲得する BC。

サブパッケージ:

- ``domain/`` — BC のドメイン語彙 (型のみ): ``AnalyzableArticle`` /
  ``IncompleteArticle`` (``complete_with_html()``) / ``PublishedAt`` /
  ``ArticleCompletionFailed`` + 長さ境界 SSoT (``article_limits``)
- ``persistence/`` — 両工程が共有する永続化資産: ``ArticleStore``
  (``articles`` 書込 + 重複判定。Pattern R 即時獲得 / Pattern H 補完獲得が共有) /
  ``StagedArticleAttributes`` (``pending_html_articles.staged_attributes`` JSONB
  契約。stage1 が書き stage2 が読む中立型)
- ``source_fetch/`` — Stage 1 (取得) 工程: ``ArticleAcquisitionService``
  (即時獲得 / 補完待ち獲得の振り分け entry point) + ``PendingHtmlEnqueue``
  (pending 投入) + 失敗ハンドリング (marker / handler / audit)
- ``article_completion/`` — Stage 2 (補完) 工程: ``ArticleCompletionService`` +
  ``ArticleHtmlExtractor`` + ``PendingHtmlQueue`` (claim / sweep / 状態遷移) +
  ``dispatch_html_fetch_jobs`` / ``sweep_expired_leases`` + retry policy
- ``fetchers/`` — 外部ソース固有の Fetcher 実装群 (Protocol)

flat モジュール:

- ``staged.py`` — ``IngestSourceArg`` (taskiq envelope)
- ``tasks.py`` — taskiq task 群 (``dispatch_sources`` / ``ingest_source`` /
  ``extract_html_body``。``dispatch_html_fetch_jobs`` / ``sweep_expired_leases``
  は ``article_completion/dispatch.py`` 側で定義)
- ``errors.py`` — ``SourceFetchError`` (Stage 1 / Stage 2 共通基底) + Stage 2 専用
  subclass (``PermanentFetchError`` / ``TemporaryFetchError`` + 4 細分化系)
- ``url_canonicalize.py`` — URL 正規化純関数
"""
