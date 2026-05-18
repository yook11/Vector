"""Stage-1 取得機構 — Source を駆動して passport を獲得・永続化する層。

「Source = 宣言 (``collection/sources/``)／ source_fetch = 実行機構」という
層分割の実行機構側。具象 ``XxxSource`` 定義は ``collection/sources/definitions/``
に在り、本パッケージは取得機構を集約する:

- ``article_fetcher`` (``ArticleSource`` を駆動する runner)
- ``protocol`` (``Fetcher`` Protocol)
- ``strategy`` (``SOURCES`` / ``FETCHERS`` レジストリ = composition root)
- ``passport_builder`` (``FetchedArticle`` → passport のドメイン品質ゲート)
- ``fetched_article`` (外部境界 DTO)
- ``tools`` (stateless I/O 道具箱: HTTP / RSS / Crossref / HN クライアント)
- ``service`` (``ArticleAcquisitionService`` = 取得→永続化ユースケース)
- 失敗ハンドリング層 (marker / handler / audit)
"""
