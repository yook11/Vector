"""Stage-1 取得機構 — Source を駆動して記事を獲得・永続化する層。

- ``strategy`` — ``SOURCES`` レジストリ
- ``fetched_article_converter`` — ``FetchedArticle`` → 獲得型の品質ゲート (総変換)
- ``fetched_article`` — 外部境界 DTO
- ``tools`` — stateless I/O 道具箱 (HTTP / RSS / Crossref / HN)
- ``service`` — 収集→変換→永続化ユースケース (唯一のオーケストレータ)
- 失敗ハンドリング層 (marker / handler / audit)
"""
