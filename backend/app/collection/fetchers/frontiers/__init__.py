"""Frontiers Media (Open Access journals) の取得 package (P2-D)。

Frontiers in Artificial Intelligence / Robotics and AI / Energy Research /
Materials の 4 journal は同型 (Frontiers Media 標準 RSS) のため取得共通処理
``frontiers_entries`` (`_common.py`) を共有する。各 journal は独立した
``FrontiersXxxSource`` クラス (`sources.py`) で、identity / 補完方針を
``ClassVar`` 宣言し ``collect(tools)`` から共通処理へ委譲する
(継承 subclass + ``JOURNAL_NAME`` ClassVar は廃止、journal 識別は
``Source.name`` に一本化)。

将来 journal を追加する場合も ``sources.py`` に 1 クラス + ``strategy.py`` の
``_SOURCES_LIST`` に 1 件 + alembic 1 行追加で済む。Frontiers の license は
全 journal CC BY 4.0 (open access policy) で統一されているため attribution は
news_sources 行で扱う。
"""
