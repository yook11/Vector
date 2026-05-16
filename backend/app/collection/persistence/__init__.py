"""collection BC の永続化アダプタ (両工程共有の小 Repository)。

domain 型 (``collection/domain/``) を ``articles`` / ``pending_html_articles``
等の物理テーブルに橋渡しする。永続化先テーブルは domain の関心事ではないため、
工程フォルダ (``source_fetch/`` / ``article_completion/``) から独立した中立な
場所として本パッケージに置く。

- ``article_store`` — ``ArticleStore`` (``articles`` 書込 + 重複判定。
  Pattern R 即時獲得 / Pattern H 補完獲得の両工程が共有)。
"""
