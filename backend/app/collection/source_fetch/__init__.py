"""Stage-1 取得機構 — Source を駆動して記事を獲得・永続化する層。

- ``article_fetcher`` — ``ArticleSource`` を駆動する runner
- ``protocol`` — ``Fetcher`` 契約
- ``strategy`` — ``SOURCES`` / ``FETCHERS`` レジストリ
- ``fetched_article_converter`` — ``FetchedArticle`` → 獲得型の品質ゲート
- ``fetched_article`` — 外部境界 DTO
- ``tools`` — stateless I/O 道具箱 (HTTP / RSS / Crossref / HN)
- ``service`` — 取得→永続化ユースケース
- 失敗ハンドリング層 (marker / handler / audit)
"""
