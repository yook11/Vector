"""per-source 知識 (identity / 補完方針 / 取得出自) の不変条件テスト。

P1 Commit 2 の核心「補完ポリシーは per-source、取得事実は per-article」を
P2 で ``ArticleSource`` 集約に移管した。本テストは実装の変更追跡ではなく
**業務不変条件**を固定する (spec §7 等価表 + composition root 契約):

1. 全登録ソースが補完知識を **無 instantiation** で公開する
   (``ArticleSource`` フィールド直読み = 副作用・ネットワーク無し)
2. 全ソースで body=html_required — 観測 body は merge を駆動しない
   (P1 は body 挙動完全不変。等価表の回帰防止の核)
3. 仮タイトルなソース (Anthropic=sitemap / ORNL=listing) は
   title=html_preferred で HTML 補完経路を強制する
   (旧 ``prefer_html_title=True`` の構造的後継)
4. 他の全ソースは default 契約 (origin=feed / DEFAULT_POLICY、
   title=observed_preferred = 旧「常に self.title」と同値)
5. 取得出自は audit 値として取得チャネルを反映する
"""

from __future__ import annotations

from app.collection.article_acquisition.strategy import SOURCES
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
    CompletableField,
    FieldCompletionRule,
)
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.source_name import SourceName

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

    def test_every_source_exposes_typed_knowledge_on_the_aggregate(self) -> None:
        """全 45 ソースが 2 属性を Source クラス属性として型整合で公開。

        クラス属性参照は ``collect`` を呼ばない (= ネットワーク・parser 構築の
        副作用を起こさない) のが要件。P2-D で ``make_adapter`` /
        ``adapter_factory`` 自体が不在のため class-ref で構造保証される。
        """
        assert len(SOURCES) == 45
        for name, source in SOURCES.items():
            origin = source.observed_origin
            profile = source.completion_policy
            assert isinstance(origin, ObservedOrigin), (
                f"{name}.observed_origin must be an ObservedOrigin"
            )
            assert isinstance(profile, ArticleCompletionPolicy), (
                f"{name}.completion_policy must be a ArticleCompletionPolicy"
            )
            # 全域性: 3 field すべてに policy がある (totality)
            assert set(profile.rules) == set(CompletableField), name


class TestFetchCadenceDeclaredOnAllSources:
    """全ソースが取得間隔 tier を宣言する (presence + 全域性)。

    ``BaseArticleSource`` に default を置かず、registry も isinstance ガードを
    持たないため、宣言漏れを実行前に捕まえるのは本テスト (と Pylance) のみ。
    全 45 件を走査し、各 ``fetch_cadence`` が ``FetchCadence`` メンバであること
    を確認する (isinstance 単独でなく登録総数も固定する)。
    """

    def test_every_source_declares_a_fetch_cadence(self) -> None:
        assert len(SOURCES) == 45
        for name, source in SOURCES.items():
            assert isinstance(source.fetch_cadence, FetchCadence), (
                f"{name}.fetch_cadence must be a FetchCadence member"
            )


class TestBodyMergeIsUnchangedAcrossAllSources:
    """spec §7 等価表: body は全ソース html_required → 観測 body は無視。"""

    def test_body_policy_is_html_required_everywhere(self) -> None:
        """観測 body を保存しても merge は HTML 由来のまま (P1 挙動不変)。"""
        for name, source in SOURCES.items():
            policy = source.completion_policy.rules[CompletableField.body]
            assert policy is FieldCompletionRule.html_required, (
                f"{name} body policy must stay html_required (merge unchanged)"
            )


class TestTitleAuthorityMatchesLegacyBehavior:
    """title policy = 旧 ``prefer_html_title`` 分岐の構造的写像。"""

    def test_provisional_title_sources_force_html_completion(self) -> None:
        """Anthropic / ORNL は title=html_preferred (旧 ``=True`` 後継)。"""
        for name in _PROVISIONAL_TITLE_SOURCES:
            profile = SOURCES[SourceName(name)].completion_policy
            assert profile is HTML_TITLE_POLICY, name
            assert (
                profile.rules[CompletableField.title]
                is FieldCompletionRule.html_preferred
            ), f"{name} must keep its provisional title overridable by HTML"

    def test_all_other_sources_keep_observed_title_authority(self) -> None:
        """非特例ソースは title=observed_preferred (旧「常に self.title」)。"""
        for name, source in SOURCES.items():
            if str(name) in _PROVISIONAL_TITLE_SOURCES:
                continue
            profile = source.completion_policy
            assert profile is DEFAULT_POLICY, (
                f"{name} must use DEFAULT_POLICY (observed title wins)"
            )
            assert (
                profile.rules[CompletableField.title]
                is FieldCompletionRule.observed_preferred
            ), name


class TestObservedOriginReflectsAcquisitionChannel:
    """origin は取得チャネルの audit 値 (merge 非駆動)。"""

    def test_special_channels_are_stamped(self) -> None:
        """Anthropic=sitemap / ORNL=listing / Hacker News=api。"""
        for name, expected in _NON_FEED_ORIGIN.items():
            assert SOURCES[SourceName(name)].observed_origin is expected, name

    def test_rss_and_default_sources_are_feed(self) -> None:
        """特例 3 件以外は feed (RSS / Atom / multi-feed machinery)。"""
        for name, source in SOURCES.items():
            if str(name) in _NON_FEED_ORIGIN:
                continue
            assert source.observed_origin is ObservedOrigin.feed, (
                f"{name} must default to ObservedOrigin.feed"
            )
