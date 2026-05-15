"""Frontiers in Robotics and AI Fetcher (Phase 3 PR 3-c-3)。

DOI prefix: ``10.3389/frobt`` (frobt = Frontiers in Robotics and AI)。
詳細は ``_common.py`` の docstring 参照。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.frontiers._common import (
    BaseFrontiersJournalAdapter,
)


class FrontiersRoboticsAIAdapter(BaseFrontiersJournalAdapter):
    NAME: ClassVar[str] = "Frontiers in Robotics and AI"
    ENDPOINT_URL: ClassVar[str] = (
        "https://www.frontiersin.org/journals/robotics-and-ai/rss"
    )
    JOURNAL_NAME: ClassVar[str] = "Frontiers in Robotics and AI"
