"""RSS entry → passport (``ReadyForArticle`` | ``IncompleteArticle``) 共通 builder。

per-source 責務は「この RSS field を body として信用するか」(= body_candidate
を渡すか None を渡すか) のみに絞り、Ready / Incomplete / drop の最終分岐は
本 builder で一手に行う。Pattern R / Pattern H という source 単位の静的
分類はこれにより消える: 同じ source でも entry ごとに「Ready 昇格 / Incomplete
保留 / drop」が決まる。

分岐契約:

- title が空 / link が空 / link canonicalize 失敗 → ``None`` (drop)
- body_candidate が ``_ARTICLE_BODY_MIN_LENGTH`` 以上 ``_ARTICLE_BODY_MAX_LENGTH``
  以下、かつ ``published_hint`` が有効な ``PublishedAt`` を組める → Ready 構築
  - ReadyForArticle の Pydantic 制約違反 → Incomplete fallback
- それ以外 → IncompleteArticle 構築 (``published_at_hint`` は組めた場合だけ載せる)

title trim は本 builder で集約 (``title.strip()[:500]``)。per-source 側で
``entry.title[:500]`` を書く必要はなくなる。
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
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


def try_build_passport(
    *,
    title: str | None,
    link: str | None,
    body_candidate: str | None,
    published_hint: datetime | None,
    source_id: int,
) -> ReadyForArticle | IncompleteArticle | None:
    """1 RSS entry を passport に変換する。

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

    if (
        body_candidate is not None
        and _ARTICLE_BODY_MIN_LENGTH <= len(body_candidate) <= _ARTICLE_BODY_MAX_LENGTH
        and published_at is not None
    ):
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
        )
    except ValueError:
        return None
