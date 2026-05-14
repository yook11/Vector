"""``strategy.py`` の整合性テスト (collection-acquisition-redesign Phase 1 完結)。"""

from __future__ import annotations

from app.collection.fetchers.hacker_news import HackerNewsFetcher
from app.collection.fetchers.strategy import FETCHERS
from app.collection.fetchers.venturebeat import VentureBeatFetcher


class TestStrategyConsistency:
    def test_all_sources_registered(self) -> None:
        """登録 fetcher 数 = 既存 20 + Phase 3 各 wave の合計 45。

        Phase 3 内訳: 3h1, 3d4, 3a, 3d1, 3b, 3d2, 3c2, 3c1, 3d3, 3e,
        3c3, 3h2, 3i1, 3c4。3-e で Cornell Chronicle 1 件、3-c-3 で
        Frontiers 4 journal を 1 PR で追加 (multi-class composition)、
        3-h-2 で METI 1 件、3-i-1 で ORNL 1 件 (BaseHtmlListingFetcher
        初導入)、3-c-4 で MDPI 4 journal を Crossref API 経路で追加。
        """
        assert len(FETCHERS) == 45

    def test_venturebeat_registered(self) -> None:
        assert FETCHERS["VentureBeat"] is VentureBeatFetcher

    def test_hacker_news_registered(self) -> None:
        assert FETCHERS["Hacker News"] is HackerNewsFetcher

    def test_factory_yields_fetcher_with_identity(self) -> None:
        for name, factory in FETCHERS.items():
            instance = factory()
            assert hasattr(instance, "fetch"), f"{name} must implement fetch"
            assert hasattr(instance, "NAME"), f"{name} must declare NAME"
            assert hasattr(instance, "ENDPOINT_URL"), (
                f"{name} must declare ENDPOINT_URL"
            )
