"""Source bounded concept — 契約 / 解決 seam / 具象定義の集約。

- ``article_source.py``: ``ArticleSource`` Protocol (1 source の構造的契約)。
  契約は具象から独立させ本パッケージ直下に置く (``FetchTools`` /
  ``FetchedArticle`` を参照するため ``domain/`` には置けない = domain が
  infra に依存しない原則)。import パスは安定 (具象 move の影響を受けない)。
- ``profile_resolver.py``: ``source_id`` / ``source_name`` →
  ``SourceCompletionProfile`` 解決 seam。repository (ACL) は本 Protocol にのみ
  依存し composition root ``SOURCES`` を import しない (spec §4.6 ガードレール 1)。
- ``definitions/``: 具象 ``XxxSource`` ×45 (1 source = 1 クラス)。

``SOURCES`` / ``FETCHERS`` レジストリ (= ``source_fetch/strategy.py``) と取得
機構 (``ArticleFetcher`` / ``Fetcher`` Protocol / ``passport_builder`` /
``tools``) は ``source_fetch/`` 側 (Stage-1 実行機構) に集約済。
"""
