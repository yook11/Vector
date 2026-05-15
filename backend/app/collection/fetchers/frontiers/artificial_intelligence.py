"""Frontiers in Artificial Intelligence Fetcher (Phase 3 PR 3-c-3)。

DOI prefix: ``10.3389/frai`` (frai = Frontiers in Artificial Intelligence)。
詳細は ``_common.py`` の docstring 参照。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.frontiers._common import (
    BaseFrontiersFetcher,
    BaseFrontiersJournalAdapter,
)


class FrontiersAIFetcher(BaseFrontiersFetcher):
    NAME: ClassVar[str] = "Frontiers in Artificial Intelligence"
    ENDPOINT_URL: ClassVar[str] = (
        "https://www.frontiersin.org/journals/artificial-intelligence/rss"
    )
    JOURNAL_NAME: ClassVar[str] = "Frontiers in Artificial Intelligence"


class FrontiersAIAdapter(BaseFrontiersJournalAdapter):
    NAME: ClassVar[str] = "Frontiers in Artificial Intelligence"
    ENDPOINT_URL: ClassVar[str] = (
        "https://www.frontiersin.org/journals/artificial-intelligence/rss"
    )
    JOURNAL_NAME: ClassVar[str] = "Frontiers in Artificial Intelligence"
