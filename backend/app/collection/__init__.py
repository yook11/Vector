"""Collection — 外部ニュースから記事を獲得する BC。

- ``domain/`` — ドメイン語彙 (型のみ)
- ``persistence/`` — 両工程共有の永続化資産
- ``article_acquisition/`` — Stage 1 (収集) 工程
- ``article_completion/`` — Stage 2 (補完) 工程
- ``sources/`` — 外部ソース固有の ``ArticleSource`` 群
- ``staged.py`` — taskiq envelope
- ``tasks.py`` — taskiq task 群
- ``errors.py`` — ソース取得失敗の例外階層
- ``url_canonicalize.py`` — URL 正規化純関数
"""
