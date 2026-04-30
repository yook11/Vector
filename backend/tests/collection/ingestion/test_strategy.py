"""``strategy.py`` の整合性テスト (collection-acquisition-redesign Phase 1a')。"""

from __future__ import annotations

from app.collection.ingestion.fetchers.venturebeat import VentureBeatFetcher
from app.collection.ingestion.strategy import (
    NEW_ROUTE_FETCHERS,
    NEW_ROUTE_SOURCE_NAMES,
)


class TestStrategyConsistency:
    def test_source_names_match_fetcher_keys(self) -> None:
        """NEW_ROUTE_SOURCE_NAMES と NEW_ROUTE_FETCHERS のキー集合は一致する。"""
        assert NEW_ROUTE_SOURCE_NAMES == frozenset(NEW_ROUTE_FETCHERS.keys())

    def test_source_names_is_frozenset(self) -> None:
        assert isinstance(NEW_ROUTE_SOURCE_NAMES, frozenset)

    def test_venturebeat_registered(self) -> None:
        assert "VentureBeat" in NEW_ROUTE_SOURCE_NAMES
        assert NEW_ROUTE_FETCHERS["VentureBeat"] is VentureBeatFetcher

    def test_factory_yields_fetcher_with_provides(self) -> None:
        for name, factory in NEW_ROUTE_FETCHERS.items():
            instance = factory()
            assert hasattr(instance, "fetch"), f"{name} must implement fetch"
            assert hasattr(instance, "PROVIDES"), f"{name} must declare PROVIDES"
            assert isinstance(instance.PROVIDES, frozenset)
