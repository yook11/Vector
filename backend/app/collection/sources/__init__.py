"""Source — ニュースソースの契約・具象定義をまとめる。

- ``article_source.py``: ``ArticleSource`` 契約
- ``definitions/``: 具象 ``XxxSource`` (1 source = 1 クラス)

profile 解決は ``app.collection.article_collection.strategy.SOURCES`` 直叩き
(spec ``Pending source identity refactor.md`` Chunk 4 で profile_resolver
adapter は廃止)。registry 未登録 source は ``KeyError`` で上位に伝播する
(``[[feedback_failure_visibility]]``)。
"""
