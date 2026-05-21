"""collection BC のドメイン語彙 (型のみ)。

- ``article_limits`` — title / body 長さ境界の SSoT。
- ``value_objects`` — ``PublishedAt`` (公開日時 VO)。
- ``analyzable_article`` — ``AnalyzableArticle`` (次工程進行保証型)。
- ``completion`` — ``ArticleCompletionFailed`` 系 (補完失敗の戻り値型)。
- ``observed_article`` — ``ObservedArticle`` / ``ObservedField`` / ``ObservedOrigin``。

具体名は各 module から fully-qualified import すること (re-export はしない)。
"""
