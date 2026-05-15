"""Frontiers in Energy Research Fetcher (Phase 3 PR 3-c-3)。

DOI prefix: ``10.3389/fenrg`` (fenrg = Frontiers in Energy Research)。
詳細は ``_common.py`` の docstring 参照。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.frontiers._common import (
    BaseFrontiersJournalAdapter,
)


class FrontiersEnergyResearchAdapter(BaseFrontiersJournalAdapter):
    NAME: ClassVar[str] = "Frontiers in Energy Research"
    ENDPOINT_URL: ClassVar[str] = (
        "https://www.frontiersin.org/journals/energy-research/rss"
    )
    JOURNAL_NAME: ClassVar[str] = "Frontiers in Energy Research"
