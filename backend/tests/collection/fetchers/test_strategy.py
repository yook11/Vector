"""``strategy.py`` の整合性テスト (fetcher big-bang リファクタ P6 cutover 後)。

P6 で 45 entry すべてが ``lambda: ArticleFetcher(XxxAdapter())`` 形に切替わった。
factory が ``ArticleFetcher`` を返し、その ``NAME`` が dispatch key と一致する
ことを構造的に固定する (cutover 漏れ = 旧 Fetcher 直参照を検出する防壁)。
"""

from __future__ import annotations

from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.strategy import FETCHERS


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

    def test_every_factory_is_adapter_driven_with_matching_identity(self) -> None:
        """全 entry が ``ArticleFetcher`` を返し ``NAME`` が key と一致する。

        P6 cutover の完了条件: factory が旧 Fetcher class 直参照のままだと
        ``ArticleFetcher`` instance にならず、ここで cutover 漏れを検出する。
        """
        for name, factory in FETCHERS.items():
            instance = factory()
            assert isinstance(instance, ArticleFetcher), (
                f"{name} must be Adapter-driven (ArticleFetcher)"
            )
            assert instance.NAME == name, (
                f"{name} key must equal Adapter.NAME (got {instance.NAME!r})"
            )
            assert instance.ENDPOINT_URL, f"{name} must declare ENDPOINT_URL"
