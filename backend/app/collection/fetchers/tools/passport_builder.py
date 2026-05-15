"""Passport (``ReadyForArticle`` | ``IncompleteArticle``) 共通 builder。

per-source 責務は「body / published を信用できる形で渡せるか」のみに絞り、
Ready / Incomplete / drop の最終分岐は本 builder で一手に行う。同じ source
でも entry ごとに「Ready 昇格 / Incomplete 保留 / drop」が決まる。

公開 API は 2 つ並存する (移行期間の並存導入):

- ``try_build_passport`` — RSS 4-tuple を直接受ける旧 API。後続 PR で
  ``SourceAdapter`` 化が完了した時点で削除予定。
- ``try_build_passport_from_fetched`` — ``FetchedArticle`` を受ける新 API。
  ``ArticleFetcher`` + ``SourceAdapter`` 経路で利用される。

両 API は private helper ``_build_passport`` に委譲し、``ReadyForArticle`` /
``IncompleteArticle`` の直接構築箇所を 1 箇所に集約する。

分岐契約:

- title が空 / link が空 / link canonicalize 失敗 → ``None`` (drop)
- body が ``_ARTICLE_BODY_MIN_LENGTH`` 以上 ``_ARTICLE_BODY_MAX_LENGTH`` 以下、
  かつ ``published`` が有効な ``PublishedAt`` を組める、かつ
  ``prefer_html_title`` が ``False`` → Ready 構築
  - ``ReadyForArticle`` の Pydantic 制約違反 → Incomplete fallback
- それ以外 → ``IncompleteArticle`` 構築 (``published_at_hint`` は組めた場合
  だけ載せる、``prefer_html_title`` も伝播)

``prefer_html_title=True`` は「現 title は仮タイトル」を表すため、body / published
が揃っていても Ready 経路は止める (HTML 補完で title 上書きの機会を残す安全弁)。

title trim は本 builder で集約 (``title.strip()[:_ARTICLE_TITLE_MAX_LENGTH]``)。
per-source 側で ``entry.title[:500]`` を書く必要はない。
"""

from __future__ import annotations

from datetime import datetime

from app.collection.article.domain.article import (
    _ARTICLE_BODY_MAX_LENGTH,
    _ARTICLE_BODY_MIN_LENGTH,
    _ARTICLE_TITLE_MAX_LENGTH,
    ReadyForArticle,
)
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


def _build_passport(
    *,
    title: str | None,
    link: str | None,
    body_candidate: str | None,
    published_hint: datetime | None,
    source_id: int,
    prefer_html_title: bool = False,
) -> ReadyForArticle | IncompleteArticle | None:
    """passport 構築の共通実装 (private)。

    両公開 API (``try_build_passport`` / ``try_build_passport_from_fetched``) が
    委譲する単一の判定ロジック。``ReadyForArticle`` / ``IncompleteArticle`` の
    直接構築はこの関数内のみで行い、公開 API が増えても構築箇所は増やさない。
    """
    if title is None:
        return None
    title_trimmed = title.strip()[:_ARTICLE_TITLE_MAX_LENGTH]
    if not title_trimmed:
        return None

    if not link:
        return None
    try:
        source_url = CanonicalArticleUrl(link)
    except ValueError:
        return None

    # tz-naive datetime は published として採用しない (PublishedAt が拒否)。
    # 採用できなかった場合は Incomplete に published_at_hint=None で流す。
    published_at: PublishedAt | None = None
    if published_hint is not None:
        try:
            published_at = PublishedAt(value=published_hint)
        except ValueError:
            published_at = None

    can_build_ready = (
        not prefer_html_title
        and body_candidate is not None
        and _ARTICLE_BODY_MIN_LENGTH <= len(body_candidate) <= _ARTICLE_BODY_MAX_LENGTH
        and published_at is not None
    )
    if can_build_ready:
        try:
            return ReadyForArticle(
                title=title_trimmed,
                body=body_candidate,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            # Ready 二次的制約違反 (title sanitize 等の domain 側 invariant) は
            # Incomplete fallback で救う。drop には落とさない (recovery 性優先)。
            pass

    try:
        return IncompleteArticle(
            title=title_trimmed,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at,
            prefer_html_title=prefer_html_title,
        )
    except ValueError:
        return None


def try_build_passport(
    *,
    title: str | None,
    link: str | None,
    body_candidate: str | None,
    published_hint: datetime | None,
    source_id: int,
) -> ReadyForArticle | IncompleteArticle | None:
    """1 RSS entry を passport に変換する (旧 API、移行期間中に並存)。

    後続 PR で ``SourceAdapter`` 化が完了した時点で削除し、
    ``try_build_passport_from_fetched`` を ``try_build_passport`` に rename
    する予定。

    Args:
        title: RSS title (plain text 化済を想定)。``None`` / 空白のみ → drop。
        link: RSS link。``None`` / 空 / canonicalize 不能 → drop。
        body_candidate: source 固有 policy で選んだ本文候補 (HTML strip 済を
            想定)。``None`` を渡すと Ready 経路は発火しない (= RSS body 不信用)。
        published_hint: feedparser 経由の UTC datetime。``None`` または
            tz-naive (``PublishedAt`` 構造違反) のとき Ready 経路は発火しない。
        source_id: Stage 1 service が解決済の ``news_sources.id``。

    Returns:
        ``ReadyForArticle`` — body + published_at が揃い品質ゲート通過
        ``IncompleteArticle`` — title + URL は揃うが Ready 条件を満たさない、
        または Ready 構築が Pydantic 制約で失敗した entry
        ``None`` — title / URL が無効で次工程に渡せない entry (drop)
    """
    return _build_passport(
        title=title,
        link=link,
        body_candidate=body_candidate,
        published_hint=published_hint,
        source_id=source_id,
        prefer_html_title=False,
    )


def try_build_passport_from_fetched(
    fetched: FetchedArticle,
    *,
    source_id: int,
) -> ReadyForArticle | IncompleteArticle | None:
    """1 ``FetchedArticle`` を passport に変換する (新 API、Adapter 経路用)。

    ``FetchedArticle`` の field は External boundary 層で空 str / ``None`` を
    用いた "不在" の表現を許容するため、本関数で str → ``None`` への正規化
    (空 str を drop シグナルに昇格) を行ってから ``_build_passport`` に渡す。

    Args:
        fetched: Adapter が外部 source から取り出した中間表現。
        source_id: Stage 1 service が解決済の ``news_sources.id``。

    Returns:
        ``ReadyForArticle`` / ``IncompleteArticle`` / ``None`` の 3 値分岐は
        ``try_build_passport`` と同じ契約。
    """
    return _build_passport(
        title=fetched.title or None,
        link=fetched.url or None,
        body_candidate=fetched.body,
        published_hint=fetched.published_at,
        source_id=source_id,
        prefer_html_title=fetched.prefer_html_title,
    )
