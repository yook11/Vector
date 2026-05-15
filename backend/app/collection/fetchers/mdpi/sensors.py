"""MDPI Sensors Fetcher / Adapter (Phase 3 PR 3-c-4 / P5)。"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.mdpi._common import (
    BaseMDPICrossrefAdapter,
)


class MDPISensorsAdapter(BaseMDPICrossrefAdapter):
    NAME: ClassVar[str] = "MDPI Sensors"
    ISSN: ClassVar[str] = "1424-8220"
    JOURNAL_NAME: ClassVar[str] = "Sensors"
