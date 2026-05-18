"""``strategy.py`` の整合性テスト (P2-D Adapter 概念除去 cutover 後)。

P2-D で ``SOURCES`` は ``SourceName → ArticleSource`` (= Source クラス
オブジェクト) のレジストリに、``FETCHERS`` は ``str(name) → (() ->
ArticleFetcher)`` 導出辞書になった。本テストは実装の変更追跡ではなく
**業務不変条件**を構造的に固定する:

1. 45 ソース全てが ``ArticleSource`` Protocol を満たすクラスオブジェクトで
   登録される
2. ``FETCHERS`` キー集合は ``str(SOURCES.key)`` と完全一致 (2 辞書 desync 排除)
3. 各 ``FETCHERS`` factory は ``ArticleFetcher`` を返し ``NAME`` が key と一致
4. **無 instantiation 契約**: ``SOURCES`` 走査 + ``completion_profile`` /
   ``observed_origin`` クラス属性読みで ``collect`` が呼ばれない
   (= 取得 machinery を構築しない。``make_adapter`` / ``adapter_factory`` 自体が
   不在のため class-ref で構造的に担保される。Stage 2 resolver が副作用無しで
   profile を引ける spec §4.6 ガードレールを設計で担保)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.collection.source_fetch.article_fetcher import ArticleFetcher
from app.collection.source_fetch.strategy import FETCHERS, SOURCES
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName


class TestStrategyConsistency:
    def test_all_sources_registered(self) -> None:
        """登録ソース数 = 既存 20 + Phase 3 各 wave の合計 45。"""
        assert len(SOURCES) == 45
        assert len(FETCHERS) == 45

    def test_every_value_is_article_source_aggregate(self) -> None:
        """``SOURCES`` の各値は ``ArticleSource`` Protocol を満たすクラス
        オブジェクトで、キーは ``name`` (``@runtime_checkable`` で構造判定)。"""
        for key, source in SOURCES.items():
            assert isinstance(source, ArticleSource), f"{key} must be ArticleSource"
            assert isinstance(key, SourceName)
            assert source.name == key, f"{key} key must equal source.name"
            assert source.endpoint_url, f"{key} must declare endpoint_url"

    def test_every_factory_is_source_driven_with_matching_identity(self) -> None:
        """全 entry が ``ArticleFetcher`` を返し ``NAME`` が key と一致する。"""
        for name, factory in FETCHERS.items():
            instance = factory()
            assert isinstance(instance, ArticleFetcher), (
                f"{name} must be Source-driven (ArticleFetcher)"
            )
            assert instance.NAME == name, (
                f"{name} key must equal ArticleFetcher.NAME (got {instance.NAME!r})"
            )
            assert instance.ENDPOINT_URL, f"{name} must declare ENDPOINT_URL"


class TestSourcesRegistryIsSingleSourceOfTruth:
    """``FETCHERS`` は ``SOURCES`` から導出される (2 辞書の desync を構造排除)。"""

    def test_fetchers_keyset_is_str_of_sources_keyset(self) -> None:
        """``FETCHERS`` キー集合は ``str(SOURCES.key)`` と完全一致する。

        ``tasks.py`` の ``FETCHERS[arg.name]`` (str キー) 消費を無改修に保つ。
        """
        assert set(FETCHERS) == {str(name) for name in SOURCES}


class TestNoInstantiationContract:
    """profile 解決経路で取得 machinery が構築されないこと (spec §4.6)。"""

    def test_reading_profile_fields_never_invokes_collect(self) -> None:
        """``SOURCES`` 走査 + profile/origin 読みで ``collect`` 非呼出。

        P2-D では ``adapter_factory`` / ``make_adapter`` 自体が不在のため
        「profile を読むのに machinery を作る」経路は class-ref で構造的に
        不能。それでも回帰防止として、各 Source クラスの ``collect`` を spy に
        差し替えた状態で全 45 件の ``completion_profile`` / ``observed_origin``
        を読み、``collect`` が一度も呼ばれないことを固定する (旧 P2 の
        ``adapter_factory`` spy を新形 retarget・構造的に強化)。
        """
        for source in SOURCES.values():
            collect_spy = MagicMock()
            with patch.object(source, "collect", collect_spy):
                # Stage 2 resolver 相当の読み取り (クラス属性直読み)
                _ = source.completion_profile
                _ = source.observed_origin
            collect_spy.assert_not_called()
