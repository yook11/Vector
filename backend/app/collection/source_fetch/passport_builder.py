"""Passport (``AnalyzableArticle`` | ``ObservedArticle``) 共通 builder。

per-source 責務は「body / published を信用できる形で渡せるか」のみに絞り、
Ready / Observed / drop の最終分岐は本 builder で一手に行う。同じ source
でも entry ごとに「Ready 昇格 / Observed 保留 / drop」が決まる。

公開 API は ``try_build_passport`` ただ 1 つ。``ArticleFetcher`` +
``ArticleSource`` 経路から ``FetchedArticle`` を受け取り passport に変換する。

本関数は private helper ``_build_passport`` に委譲し、``AnalyzableArticle`` /
``ObservedArticle`` の直接構築箇所を 1 箇所に集約する。

分岐契約:

- title が空 / link が空 / link canonicalize 失敗 → ``None`` (drop)
- body が ``ARTICLE_BODY_MIN_LENGTH`` 以上 ``ARTICLE_BODY_MAX_LENGTH`` 以下、
  かつ ``published`` が有効な ``PublishedAt`` を組める、かつ profile が
  Stage-1 Ready を構造的に阻害しない (どの analyzable field も
  ``html_preferred`` を持たない) → Ready 構築
  - ``AnalyzableArticle`` の Pydantic 制約違反 → Observed fallback
- それ以外 → ``ObservedArticle`` 構築 (**取れた事実は全部保存**:
  title / body / published_at を存在する限り ``ObservedField`` に詰める。
  要否 / 優先は Stage 2 で ``SourceCompletionProfile`` が決める)

profile のいずれかの analyzable field が ``html_preferred`` のとき (= その
field の正本は Stage-2 HTML 経由でしか確定しないプレースホルダ)、body /
published が揃っていても Ready 経路を止め ``ObservedArticle`` 保留に落とす
(HTML 補完で正本上書きの機会を残す安全弁)。判定は title hardcode ではなく
``SourceCompletionProfile.precludes_stage1_ready()`` への per-field 委譲で、
per-source の仮タイトル性を profile が所有する。

title trim は本 builder で集約 (``title.strip()[:ARTICLE_TITLE_MAX_LENGTH]``)。
per-source 側で ``entry.title[:500]`` を書く必要はない。
"""

from __future__ import annotations

from datetime import datetime

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
)
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.source_name import SourceName


def _build_passport(
    *,
    title: str | None,
    link: str | None,
    body_candidate: str | None,
    published_hint: datetime | None,
    source_id: int,
    source_name: SourceName,
    origin: ObservedOrigin,
    ready_precluded: bool = False,
) -> AnalyzableArticle | ObservedArticle | None:
    """passport 構築の共通実装 (private)。

    公開 API ``try_build_passport`` が委譲する単一の判定ロジック。
    ``AnalyzableArticle`` / ``ObservedArticle`` の直接構築はこの関数内のみで
    行い、構築箇所を 1 箇所に閉じ込める。
    """
    if title is None:
        return None
    title_trimmed = title.strip()[:ARTICLE_TITLE_MAX_LENGTH]
    if not title_trimmed:
        return None

    if not link:
        return None
    try:
        source_url = CanonicalArticleUrl(link)
    except ValueError:
        return None

    # tz-naive datetime は published として採用しない (PublishedAt が拒否)。
    # 採用できなかった場合は Observed に published_at=None で流す。
    published_at: PublishedAt | None = None
    if published_hint is not None:
        try:
            published_at = PublishedAt(value=published_hint)
        except ValueError:
            published_at = None

    can_build_ready = (
        not ready_precluded
        and body_candidate is not None
        and ARTICLE_BODY_MIN_LENGTH <= len(body_candidate) <= ARTICLE_BODY_MAX_LENGTH
        and published_at is not None
    )
    if can_build_ready:
        try:
            return AnalyzableArticle(
                title=title_trimmed,
                body=body_candidate,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            # Ready 二次的制約違反 (title sanitize 等の domain 側 invariant) は
            # Observed fallback で救う。drop には落とさない (recovery 性優先)。
            pass

    # 取れた事実は全部保存する (原則: 観測は全部保存し、要否は profile が決める)。
    # body は全現行ソースで html_required のため merge では無視されるが、観測
    # された事実としては保持する (forward-compat。挙動は不変 — spec §7 等価表)。
    try:
        return ObservedArticle(
            source_name=source_name,
            source_url=source_url,
            title=ObservedField(value=title_trimmed, origin=origin),
            body=(
                ObservedField(value=body_candidate, origin=origin)
                if body_candidate
                else None
            ),
            published_at=(
                ObservedField(value=published_at, origin=origin)
                if published_at is not None
                else None
            ),
        )
    except ValueError:
        return None


def try_build_passport(
    fetched: FetchedArticle,
    *,
    source_id: int,
    source_name: SourceName,
    origin: ObservedOrigin,
    profile: SourceCompletionProfile = DEFAULT_PROFILE,
) -> AnalyzableArticle | ObservedArticle | None:
    """1 ``FetchedArticle`` を passport に変換する (Source 経路の唯一の builder)。

    ``FetchedArticle`` の field は External boundary 層で空 str / ``None`` を
    用いた "不在" の表現を許容するため、本関数で str → ``None`` への正規化
    (空 str を drop シグナルに昇格) を行ってから ``_build_passport`` に渡す。

    profile のいずれかの analyzable field が ``html_preferred`` のとき
    (その field の正本は Stage-2 HTML でしか確定しない) Ready 経路を止める。
    判定は title hardcode ではなく ``profile.precludes_stage1_ready()`` への
    per-field 委譲で、source 固有 flag を中間型に持たせない (R/H 分岐は不変)。

    Args:
        fetched: Source が外部 source から取り出した中間表現。
        source_id: Stage 1 service が解決済の ``news_sources.id`` (Ready 経路の
            ``AnalyzableArticle`` が原産 FK として持つ。Observed 経路の
            identity は pending 行の関心で enqueue 時に注入される)。
        source_name: ソース表示名 (``ArticleSource.name``)。観測事実の出所。
        origin: 取得チャネル (``ArticleSource.observed_origin``)。``ObservedField``
            に stamp する audit 値 (merge は駆動しない)。
        profile: ``Source`` の補完方針 (html_preferred field の有無で
            Stage-1 Ready gate を決める)。

    Returns:
        ``AnalyzableArticle`` — body + published_at が揃い品質ゲート通過
        ``ObservedArticle`` — title + URL は揃うが Ready 条件を満たさない、
        または Ready 構築が Pydantic 制約で失敗した entry (取れた事実を全保存)
        ``None`` — title / URL が無効で次工程に渡せない entry (drop)
    """
    return _build_passport(
        title=fetched.title or None,
        link=fetched.url or None,
        body_candidate=fetched.body,
        published_hint=fetched.published_at,
        source_id=source_id,
        source_name=source_name,
        origin=origin,
        ready_precluded=profile.precludes_stage1_ready(),
    )
