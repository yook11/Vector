"""per-source 知識 (補完方針 / 取得出自) の不変条件テスト。

P1 Commit 2 の核心は「補完ポリシーは per-source、取得事実は per-article」を
``SourceAdapter`` ClassVar に集約したこと。本テストは実装の変更追跡ではなく
**業務不変条件**を固定する (spec §7 等価表 + composition root 契約):

1. 全登録ソースが補完知識を **無 instantiation** で公開する
   (Stage 2 resolver は副作用・ネットワーク無しで profile を引ける)
2. 全ソースで body=html_required — 観測 body は merge を駆動しない
   (P1 は body 挙動完全不変。等価表の回帰防止の核)
3. 仮タイトルなソース (Anthropic=sitemap / ORNL=listing) は
   title=html_preferred で HTML 補完経路を強制する
   (旧 ``prefer_html_title=True`` の構造的後継)
4. 他の全ソースは default 契約 (origin=feed / DEFAULT_PROFILE、
   title=observed_preferred = 旧「常に self.title」と同値)
5. 取得出自は audit 値として取得チャネルを反映する
"""

from __future__ import annotations

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    HTML_TITLE_PROFILE,
    AnalyzableField,
    FieldCompletionPolicy,
    SourceCompletionProfile,
)
from app.collection.fetchers.strategy import SOURCES

# title が「仮」のため HTML 補完で上書きさせるソース (spec 特例)。
_PROVISIONAL_TITLE_SOURCES = {"Anthropic", "ORNL"}
# 取得出自が feed 以外の特例 (audit only)。
_NON_FEED_ORIGIN = {
    "Anthropic": ObservedOrigin.sitemap,
    "ORNL": ObservedOrigin.listing,
    "Hacker News": ObservedOrigin.api,
}


class TestCompletionKnowledgeIsRegistryReachable:
    """resolver が無 instantiation で per-source 知識を引けること。"""

    def test_every_source_exposes_typed_knowledge_on_the_class(self) -> None:
        """全 45 ソースが 2 属性をクラス属性として型整合で公開する。

        ``adapter_cls.completion_profile`` の参照で instance を作らない
        (= ネットワーク・parser 構築の副作用を起こさない) のが要件。
        """
        assert len(SOURCES) == 45
        for name, adapter_cls in SOURCES.items():
            origin = adapter_cls.observed_origin
            profile = adapter_cls.completion_profile
            assert isinstance(origin, ObservedOrigin), (
                f"{name}.observed_origin must be an ObservedOrigin"
            )
            assert isinstance(profile, SourceCompletionProfile), (
                f"{name}.completion_profile must be a SourceCompletionProfile"
            )
            # 全域性: 3 field すべてに policy がある (totality)
            assert set(profile.policies) == set(AnalyzableField), name


class TestBodyMergeIsUnchangedAcrossAllSources:
    """spec §7 等価表: body は全ソース html_required → 観測 body は無視。"""

    def test_body_policy_is_html_required_everywhere(self) -> None:
        """観測 body を保存しても merge は HTML 由来のまま (P1 挙動不変)。"""
        for name, adapter_cls in SOURCES.items():
            policy = adapter_cls.completion_profile.policies[AnalyzableField.body]
            assert policy is FieldCompletionPolicy.html_required, (
                f"{name} body policy must stay html_required (merge unchanged)"
            )


class TestTitleAuthorityMatchesLegacyBehavior:
    """title policy = 旧 ``prefer_html_title`` 分岐の構造的写像。"""

    def test_provisional_title_sources_force_html_completion(self) -> None:
        """Anthropic / ORNL は title=html_preferred (旧 ``=True`` 後継)。"""
        for name in _PROVISIONAL_TITLE_SOURCES:
            profile = SOURCES[name].completion_profile
            assert profile is HTML_TITLE_PROFILE, name
            assert (
                profile.policies[AnalyzableField.title]
                is FieldCompletionPolicy.html_preferred
            ), f"{name} must keep its provisional title overridable by HTML"

    def test_all_other_sources_keep_observed_title_authority(self) -> None:
        """非特例ソースは title=observed_preferred (旧「常に self.title」)。"""
        for name, adapter_cls in SOURCES.items():
            if name in _PROVISIONAL_TITLE_SOURCES:
                continue
            profile = adapter_cls.completion_profile
            assert profile is DEFAULT_PROFILE, (
                f"{name} must use DEFAULT_PROFILE (observed title wins)"
            )
            assert (
                profile.policies[AnalyzableField.title]
                is FieldCompletionPolicy.observed_preferred
            ), name


class TestObservedOriginReflectsAcquisitionChannel:
    """origin は取得チャネルの audit 値 (merge 非駆動)。"""

    def test_special_channels_are_stamped(self) -> None:
        """Anthropic=sitemap / ORNL=listing / Hacker News=api。"""
        for name, expected in _NON_FEED_ORIGIN.items():
            assert SOURCES[name].observed_origin is expected, name

    def test_rss_and_default_sources_are_feed(self) -> None:
        """特例 3 件以外は feed (RSS / Atom / 継承 base default)。"""
        for name, adapter_cls in SOURCES.items():
            if name in _NON_FEED_ORIGIN:
                continue
            assert adapter_cls.observed_origin is ObservedOrigin.feed, (
                f"{name} must default to ObservedOrigin.feed"
            )
