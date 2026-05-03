"""``strategy.py`` の整合性テスト (collection-acquisition-redesign Phase 1 完結)。"""

from __future__ import annotations

from app.collection.ingestion.fetchers.hacker_news import HackerNewsFetcher
from app.collection.ingestion.fetchers.venturebeat import VentureBeatFetcher
from app.collection.ingestion.strategy import FETCHERS


class TestStrategyConsistency:
    def test_all_sources_registered(self) -> None:
        """既存 20 + Phase 3 (3-h-1 + 3-d-4 + 3-a + 3-d-1) = 27 sources."""
        assert len(FETCHERS) == 27

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
