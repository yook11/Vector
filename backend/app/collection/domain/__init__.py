"""collection BC のドメイン語彙 (型のみ)。

外部ニュースから「分析工程への進行が型で保証された記事」を獲得する BC の
中心語彙をここに集約する。永続化先テーブルは domain の関心事ではない
(``persistence/`` が担う)。

- ``article_limits`` — title / body 長さ境界の SSoT (4 公開定数)。
- ``value_objects`` — ``PublishedAt`` (tzinfo=UTC を構造保証する公開日時 VO)。
- ``analyzable_article`` — ``AnalyzableArticle`` (次工程進行保証型 passport)。
- ``completion`` — ``ArticleCompletionFailed`` 系 (補完失敗の戻り値型)。
- ``incomplete_article`` — ``IncompleteArticle`` Entity +
  ``complete_with_html`` (未完成 → 完成の唯一の純粋遷移)。

具体名は各 module から fully-qualified import すること (re-export はしない)。
"""
