"""``incomplete_article`` aggregate — HTML 補完待ちの未完成記事。

RSS 本文が短い (Pattern H) 場合、fetcher は ``IncompleteArticle`` を返し
``pending_html_articles`` に lease ベースで保存される。後段の HTML 取得 +
補完 (``IncompleteArticle.complete_with_html``) が成功すれば ``Article`` へ
遷移、失敗すれば ``ArticleCompletionFailed`` で監査される。

- :mod:`.domain.incomplete_article` — ``IncompleteArticle`` Entity
- :mod:`.domain.staged_attributes` — ``StagedArticleAttributes``
- :mod:`.domain.completion` — ``ArticleCompletionFailed`` 系
- :mod:`.repository` — ``PendingHtmlArticleRepository``
"""
