"""MDPI Sensors Fetcher (Phase 3 PR 3-c-4)。"""

from __future__ import annotations

from typing import ClassVar

from app.collection.ingestion.fetchers.mdpi._common import BaseMDPICrossrefFetcher


class MDPISensorsFetcher(BaseMDPICrossrefFetcher):
    NAME: ClassVar[str] = "MDPI Sensors"
    ISSN: ClassVar[str] = "1424-8220"
    JOURNAL_NAME: ClassVar[str] = "Sensors"
