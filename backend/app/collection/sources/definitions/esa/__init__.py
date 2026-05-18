"""ESA Djangoplicity 規格 RSS の取得 package (P2-D)。

ESA/Hubble + ESA/Webb は同型 (Djangoplicity News module) のため取得共通処理
``djangoplicity_entries`` (`_common.py`) を共有する。各ソースは独立した
``ESAHubbleSource`` / ``ESAWebbSource`` クラス (`sources.py`) で、identity /
補完方針を ``ClassVar`` 宣言し ``collect(tools)`` から共通処理へ委譲する
(継承 subclass は廃止)。

将来 ESO / ALMA など他の ESA Djangoplicity 系を追加する場合も、``sources.py``
に 1 クラス追加 + ``strategy.py`` の ``_SOURCES_LIST`` に 1 件追加で済む。
"""
