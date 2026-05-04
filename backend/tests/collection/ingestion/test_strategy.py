"""``strategy.py`` の整合性テスト (collection-acquisition-redesign Phase 1 完結)。"""

from __future__ import annotations

from app.collection.ingestion.fetchers.hacker_news import HackerNewsFetcher
from app.collection.ingestion.fetchers.venturebeat import VentureBeatFetcher
from app.collection.ingestion.strategy import FETCHERS


class TestStrategyConsistency:
    def test_all_sources_registered(self) -> None:
        """既存 20 + Phase 3 (3h1+3d4+3a+3d1+3b+3d2+3c2+3c1+3d3+3e+3c3+3h2+3i1) = 41.

        3-e で Cornell Chronicle 1 件、3-c-3 で Frontiers 4 journal を
        1 PR で追加 (multi-class composition)、3-h-2 で METI 1 件、
        3-i-1 で ORNL 1 件 (BaseHtmlListingFetcher 初導入)。
        """
        assert len(FETCHERS) == 41

    def test_venturebeat_registered(self) -> None:
        assert FETCHERS["VentureBeat"] is VentureBeatFetcher

    def test_hacker_news_registered(self) -> None:
        assert FETCHERS["Hacker News"] is HackerNewsFetcher

    def test_factory_yields_fetcher_with_provides(self) -> None:
        for name, factory in FETCHERS.items():
            instance = factory()
            assert hasattr(instance, "fetch"), f"{name} must implement fetch"
            assert hasattr(instance, "PROVIDES"), f"{name} must declare PROVIDES"
            assert isinstance(instance.PROVIDES, frozenset)
