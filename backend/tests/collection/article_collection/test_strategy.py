"""``strategy.py`` の整合性テスト (Fetcher 解体 cutover 後)。

``SOURCES`` は ``SourceName → ArticleSource`` (= Source クラスオブジェクト) の
唯一の dispatch レジストリ (Fetcher 解体で ``FETCHERS`` は撤去)。本テストは
実装の変更追跡ではなく **業務不変条件**を構造的に固定する:

1. 45 ソース全てが ``ArticleSource`` Protocol を満たすクラスオブジェクトで
   登録される
2. キーは ``str(name)`` で dispatch でき ``source.name`` と一致する
   (``tasks.py`` の ``SOURCES[SourceName(arg.name)]`` 消費を支える)
3. **無 instantiation 契約**: ``SOURCES`` 走査 + ``completion_policy`` /
   ``observed_origin`` クラス属性読みで ``collect`` が呼ばれない
   (= 取得 machinery を構築しない。``make_adapter`` / ``adapter_factory`` 自体が
   不在のため class-ref で構造的に担保される。Stage 2 resolver が副作用無しで
   profile を引ける spec §4.6 ガードレールを設計で担保)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.collection.article_collection.strategy import SOURCES
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName


class TestStrategyConsistency:
    def test_all_sources_registered(self) -> None:
        """登録ソース数 = 既存 20 + Phase 3 各 wave の合計 45。"""
        assert len(SOURCES) == 45

    def test_every_value_is_article_source_aggregate(self) -> None:
        """``SOURCES`` の各値は ``ArticleSource`` Protocol を満たすクラス
        オブジェクトで、キーは ``name`` (``@runtime_checkable`` で構造判定)。"""
        for key, source in SOURCES.items():
            assert isinstance(source, ArticleSource), f"{key} must be ArticleSource"
            assert isinstance(key, SourceName)
            assert source.name == key, f"{key} key must equal source.name"
            assert source.endpoint_url, f"{key} must declare endpoint_url"

    def test_str_name_dispatch_round_trips(self) -> None:
        """``str(name)`` から ``SourceName`` 経由で同一 source を引ける。

        ``tasks.py`` の ``SOURCES[SourceName(arg.name)]`` (arg.name は str) の
        dispatch を無改修に保つ。
        """
        for key, source in SOURCES.items():
            assert SOURCES[SourceName(str(key))] is source


class TestNoInstantiationContract:
    """profile 解決経路で取得 machinery が構築されないこと (spec §4.6)。"""

    def test_reading_profile_fields_never_invokes_collect(self) -> None:
        """``SOURCES`` 走査 + profile/origin 読みで ``collect`` 非呼出。

        P2-D では ``adapter_factory`` / ``make_adapter`` 自体が不在のため
        「profile を読むのに machinery を作る」経路は class-ref で構造的に
        不能。それでも回帰防止として、各 Source クラスの ``collect`` を spy に
        差し替えた状態で全 45 件の ``completion_policy`` / ``observed_origin``
        を読み、``collect`` が一度も呼ばれないことを固定する (旧 P2 の
        ``adapter_factory`` spy を新形 retarget・構造的に強化)。
        """
        for source in SOURCES.values():
            collect_spy = MagicMock()
            with patch.object(source, "collect", collect_spy):
                # Stage 2 resolver 相当の読み取り (クラス属性直読み)
                _ = source.completion_policy
                _ = source.observed_origin
            collect_spy.assert_not_called()
