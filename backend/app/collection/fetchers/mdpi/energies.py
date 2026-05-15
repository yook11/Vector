"""MDPI Energies Fetcher / Adapter (Phase 3 PR 3-c-4 / P5)。"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.mdpi._common import (
    BaseMDPICrossrefAdapter,
    BaseMDPICrossrefFetcher,
)


class MDPIEnergiesFetcher(BaseMDPICrossrefFetcher):
    NAME: ClassVar[str] = "MDPI Energies"
    ISSN: ClassVar[str] = "1996-1073"
    JOURNAL_NAME: ClassVar[str] = "Energies"


class MDPIEnergiesAdapter(BaseMDPICrossrefAdapter):
    NAME: ClassVar[str] = "MDPI Energies"
    ISSN: ClassVar[str] = "1996-1073"
    JOURNAL_NAME: ClassVar[str] = "Energies"
