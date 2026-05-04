"""MDPI Nanomaterials Fetcher (Phase 3 PR 3-c-4)。"""

from __future__ import annotations

from typing import ClassVar

from app.collection.ingestion.fetchers.mdpi._common import BaseMDPICrossrefFetcher


class MDPINanomaterialsFetcher(BaseMDPICrossrefFetcher):
    NAME: ClassVar[str] = "MDPI Nanomaterials"
    ISSN: ClassVar[str] = "2079-4991"
    JOURNAL_NAME: ClassVar[str] = "Nanomaterials"
