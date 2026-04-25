"""Deprecated: ``PublishedAt`` は ``domain.value_objects`` に移管済み。

このファイルは PR 2b で削除予定。互換性のため再 export のみ残す。
新規コードは :mod:`app.collection.extraction.domain.value_objects` から
直接 import すること。
"""

from app.collection.extraction.domain.value_objects import PublishedAt as PublishedAt

__all__ = ["PublishedAt"]
