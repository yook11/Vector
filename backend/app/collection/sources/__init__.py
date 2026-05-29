"""Source — ニュースソースの契約・具象定義をまとめる。

- ``article_source.py``: ``ArticleSource`` 契約
- ``definitions/``: 具象 ``XxxSource`` (1 source = 1 クラス)

acquisition task の profile 解決は
``app.collection.article_acquisition.strategy.SOURCES`` 直叩き (spec
``Pending source identity refactor.md`` Chunk 4 で profile_resolver adapter は廃止)。
registry 未登録 source は trusted dispatch invariant 違反として ``KeyError`` で
上位に伝播する。一方、永続 pending を読む completion 経路では registry helper が
``SourceNotRegisteredError`` に変換し、監査可能な実行時 failure として扱う。
"""
