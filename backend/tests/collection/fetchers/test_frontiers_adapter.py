"""Frontiers Adapter 群 (base + 4 thin subclass) の単体テスト (HTTP 非依存)。

固定する固有不変条件:

- ``description`` が 50 chars 未満の editorial/correction entry は
  ``BaseFrontiersJournalAdapter.collect()`` 内で business critical drop される
  (旧 ``BaseFrontiersFetcher`` の body<50 drop 移植証明)
- 4 subclass の ``NAME`` / ``ENDPOINT_URL`` / ``JOURNAL_NAME`` ClassVar が
  期待値と一致する (MDPI 形 thin subclass の整合)
"""

from __future__ import annotations

from pathlib import Path

import feedparser
import pytest

from app.collection.fetchers.frontiers.artificial_intelligence import (
    FrontiersAIAdapter,
)
from app.collection.fetchers.frontiers.energy_research import (
    FrontiersEnergyResearchAdapter,
)
from app.collection.fetchers.frontiers.materials import FrontiersMaterialsAdapter
from app.collection.fetchers.frontiers.robotics_and_ai import (
    FrontiersRoboticsAIAdapter,
)
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_FIXTURE = "frontiers_ai_rss.xml"


class _FakeRssParser:
    def __init__(self, fixture_filename: str) -> None:
        self._fixture_filename = fixture_filename

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        path = _FIXTURES_DIR / self._fixture_filename
        feed = feedparser.parse(path.read_bytes())
        return [normalize_entry(raw) for raw in feed.entries]


async def test_short_description_entry_is_dropped() -> None:
    """fixture は通常記事 (desc≈613) + Editorial (desc=16) の 2 件。
    body<50 の Editorial は drop され通常記事のみ yield される。"""
    adapter = FrontiersAIAdapter(parser=_FakeRssParser(_FIXTURE))  # type: ignore[arg-type]

    items: list[FetchedArticle] = [item async for item in adapter.collect()]

    assert len(items) == 1
    assert all(item.body is not None and len(item.body) >= 50 for item in items)
    assert not any(item.title.startswith("Editorial:") for item in items)


@pytest.mark.parametrize(
    ("adapter_cls", "name", "endpoint_url", "journal_name"),
    [
        (
            FrontiersAIAdapter,
            "Frontiers in Artificial Intelligence",
            "https://www.frontiersin.org/journals/artificial-intelligence/rss",
            "Frontiers in Artificial Intelligence",
        ),
        (
            FrontiersRoboticsAIAdapter,
            "Frontiers in Robotics and AI",
            "https://www.frontiersin.org/journals/robotics-and-ai/rss",
            "Frontiers in Robotics and AI",
        ),
        (
            FrontiersEnergyResearchAdapter,
            "Frontiers in Energy Research",
            "https://www.frontiersin.org/journals/energy-research/rss",
            "Frontiers in Energy Research",
        ),
        (
            FrontiersMaterialsAdapter,
            "Frontiers in Materials",
            "https://www.frontiersin.org/journals/materials/rss",
            "Frontiers in Materials",
        ),
    ],
)
def test_subclass_classvars(
    adapter_cls: type,
    name: str,
    endpoint_url: str,
    journal_name: str,
) -> None:
    assert adapter_cls.NAME == name
    assert adapter_cls.ENDPOINT_URL == endpoint_url
    assert adapter_cls.JOURNAL_NAME == journal_name
