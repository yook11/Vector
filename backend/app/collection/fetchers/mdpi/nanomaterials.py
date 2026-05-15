"""MDPI Nanomaterials Fetcher / Adapter (Phase 3 PR 3-c-4 / P5)。"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.mdpi._common import (
    BaseMDPICrossrefAdapter,
    BaseMDPICrossrefFetcher,
)


class MDPINanomaterialsFetcher(BaseMDPICrossrefFetcher):
    NAME: ClassVar[str] = "MDPI Nanomaterials"
    ISSN: ClassVar[str] = "2079-4991"
    JOURNAL_NAME: ClassVar[str] = "Nanomaterials"


class MDPINanomaterialsAdapter(BaseMDPICrossrefAdapter):
    NAME: ClassVar[str] = "MDPI Nanomaterials"
    ISSN: ClassVar[str] = "2079-4991"
    JOURNAL_NAME: ClassVar[str] = "Nanomaterials"
