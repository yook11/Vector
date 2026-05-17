"""Frontiers Media (Open Access journals) の取得 machinery package (P2)。

Frontiers in Artificial Intelligence / Robotics and AI / Energy Research /
Materials の 4 journal は同型 (Frontiers Media 標準 RSS) のため
``FrontiersJournalAdapter`` (`_common.py`) 汎用 machinery を共有する。P2 で
per-source の identity (``name`` / ``endpoint_url``) と補完方針は
``ArticleSource`` 集約 (`fetchers/strategy.py`) が所有し、各 journal は
``ArticleSource.adapter_factory`` から本 machinery を構築する
(継承 subclass + ``JOURNAL_NAME`` ClassVar は廃止、journal 識別は
``ArticleSource.name`` に一本化)。

将来 journal を追加する場合も ``strategy.py`` に ``ArticleSource`` を 1 件 +
alembic 1 行追加で済む。Frontiers の license は全 journal CC BY 4.0
(open access policy) で統一されているため attribution は news_sources 行で
扱う。
"""
