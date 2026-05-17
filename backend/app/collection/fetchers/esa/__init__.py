"""ESA Djangoplicity 規格 RSS の取得 machinery package (P2)。

ESA/Hubble + ESA/Webb は同型 (Djangoplicity News module) のため
``DjangoplicityAdapter`` (`_common.py`) 汎用 machinery を共有する。P2 で
per-source の identity (``name`` / ``endpoint_url``) と補完方針は
``ArticleSource`` 集約 (`fetchers/strategy.py`) が所有し、各ソースは
``ArticleSource.adapter_factory`` から本 machinery を構築する
(継承 subclass は廃止)。

将来 ESO / ALMA など他の ESA Djangoplicity 系を追加する場合も、
``strategy.py`` に ``DjangoplicityAdapter`` を factory とする ``ArticleSource``
を 1 件追加するだけで済む。
"""
