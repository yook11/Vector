"""4 Frontiers subclass の identity / 契約検証。

per-source の振る舞いは ``test__common.py`` で base の dummy subclass を通じて
網羅済み。本ファイルは subclass が dispatch キー (NAME / ENDPOINT_URL) で衝突
しないことと、共通契約 (PROVIDES / URL pattern) を正しく継承していることだけを
保証する。
"""

from __future__ import annotations

from app.collection.ingestion.fetchers.frontiers._common import BaseFrontiersFetcher
from app.collection.ingestion.fetchers.frontiers.artificial_intelligence import (
    FrontiersAIFetcher,
)
from app.collection.ingestion.fetchers.frontiers.energy_research import (
    FrontiersEnergyResearchFetcher,
)
from app.collection.ingestion.fetchers.frontiers.materials import (
    FrontiersMaterialsFetcher,
)
from app.collection.ingestion.fetchers.frontiers.robotics_and_ai import (
    FrontiersRoboticsAIFetcher,
)

_ALL = (
    FrontiersAIFetcher,
    FrontiersRoboticsAIFetcher,
    FrontiersEnergyResearchFetcher,
    FrontiersMaterialsFetcher,
)


def test_all_subclass_base_fetcher() -> None:
    for cls in _ALL:
        assert issubclass(cls, BaseFrontiersFetcher)


def test_provides_inherited_consistently() -> None:
    expected = frozenset({"language", "guid", "site_name", "author"})
    for cls in _ALL:
        assert cls.PROVIDES == expected


def test_endpoints_follow_frontiers_url_pattern() -> None:
    for cls in _ALL:
        assert cls.ENDPOINT_URL.startswith("https://www.frontiersin.org/journals/")
        assert cls.ENDPOINT_URL.endswith("/rss")


def test_subclasses_have_distinct_dispatch_keys() -> None:
    """``NAME`` / ``ENDPOINT_URL`` は composition root の dispatch dict キー。"""
    assert len({cls.NAME for cls in _ALL}) == len(_ALL)
    assert len({cls.ENDPOINT_URL for cls in _ALL}) == len(_ALL)
