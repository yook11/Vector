"""4 subclass の ClassVar 構成確認 (Phase 3 PR 3-c-3)。

各 subclass が NAME / ENDPOINT_URL / JOURNAL_NAME を正しく差し替えていることと、
PROVIDES が共通 frozenset で、ENDPOINT_URL が Frontiers の正規 URL pattern に
従うことを確認する。
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


class TestSubclassConfiguration:
    def test_all_subclass_base(self) -> None:
        for cls in _ALL:
            assert issubclass(cls, BaseFrontiersFetcher)

    def test_provides_inherited(self) -> None:
        # PROVIDES は base から継承される共通 frozenset
        for cls in _ALL:
            assert cls.PROVIDES == frozenset(
                {"language", "guid", "site_name", "author"}
            )

    def test_endpoint_pattern(self) -> None:
        # 全 subclass が Frontiers 正規 URL pattern に従う
        for cls in _ALL:
            assert cls.ENDPOINT_URL.startswith("https://www.frontiersin.org/journals/")
            assert cls.ENDPOINT_URL.endswith("/rss")

    def test_unique_names(self) -> None:
        names = {cls.NAME for cls in _ALL}
        assert len(names) == 4

    def test_unique_endpoints(self) -> None:
        endpoints = {cls.ENDPOINT_URL for cls in _ALL}
        assert len(endpoints) == 4

    def test_journal_name_matches_name(self) -> None:
        # Frontiers は NAME と JOURNAL_NAME が同値で運用 (Cornell 等と差異なし)
        for cls in _ALL:
            assert cls.NAME == cls.JOURNAL_NAME

    def test_ai_fetcher_specifics(self) -> None:
        assert FrontiersAIFetcher.NAME == "Frontiers in Artificial Intelligence"
        assert "artificial-intelligence" in FrontiersAIFetcher.ENDPOINT_URL

    def test_robotics_fetcher_specifics(self) -> None:
        assert FrontiersRoboticsAIFetcher.NAME == "Frontiers in Robotics and AI"
        assert "robotics-and-ai" in FrontiersRoboticsAIFetcher.ENDPOINT_URL

    def test_energy_fetcher_specifics(self) -> None:
        assert FrontiersEnergyResearchFetcher.NAME == "Frontiers in Energy Research"
        assert "energy-research" in FrontiersEnergyResearchFetcher.ENDPOINT_URL

    def test_materials_fetcher_specifics(self) -> None:
        assert FrontiersMaterialsFetcher.NAME == "Frontiers in Materials"
        assert "/journals/materials/" in FrontiersMaterialsFetcher.ENDPOINT_URL
