"""Frontiers Media (Open Access journals) per-journal fetcher 群 (Phase 3 PR 3-c-3)。

Frontiers in Artificial Intelligence / Robotics and AI / Energy Research /
Materials の 4 journal は同型 (Frontiers Media 標準 RSS) のため
``BaseFrontiersFetcher`` (`_common.py`) を共有し、subclass で ClassVar
(``NAME`` / ``ENDPOINT_URL`` / ``JOURNAL_NAME``) のみ差し替える。

将来 journal を追加する場合も同 base 上で ClassVar 差し替え + alembic 1 行
追加で済む。Frontiers の license は全 journal CC BY 4.0 (open access policy)
で統一されているため `_common.py` で hardcode する。
"""
