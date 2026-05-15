"""MDPI Materials Fetcher / Adapter (Phase 3 PR 3-c-4 / P5)。"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.mdpi._common import (
    BaseMDPICrossrefAdapter,
    BaseMDPICrossrefFetcher,
)


class MDPIMaterialsFetcher(BaseMDPICrossrefFetcher):
    NAME: ClassVar[str] = "MDPI Materials"
    ISSN: ClassVar[str] = "1996-1944"
    JOURNAL_NAME: ClassVar[str] = "Materials"


class MDPIMaterialsAdapter(BaseMDPICrossrefAdapter):
    NAME: ClassVar[str] = "MDPI Materials"
    ISSN: ClassVar[str] = "1996-1944"
    JOURNAL_NAME: ClassVar[str] = "Materials"
