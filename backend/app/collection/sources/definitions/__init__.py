"""具象 ``XxxSource`` 定義の集約 (1 source = 1 クラス)。

各モジュールが 1 ニュースソースを宣言的に定義する: identity
(``name`` / ``endpoint_url`` / ``observed_origin``) + 補完方針
(``completion_profile``) + 取得手順 (``collect(tools)``)。これらは
``sources/article_source.py`` の ``ArticleSource`` Protocol を構造的に満たす
(継承はしない)。契約 (Protocol) は本パッケージ外の ``sources/`` 直下に独立
させ、本パッケージは実体のみを束ねる。
"""
