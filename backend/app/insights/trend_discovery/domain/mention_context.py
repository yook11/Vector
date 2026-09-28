"""共起記事数に基づいて関連メンションを選ぶ。"""

from __future__ import annotations

from collections.abc import Iterable

from app.insights.trend_discovery.domain.trend import (
    MAX_RELATED_MENTIONS,
    MentionKey,
    RelatedMention,
)


def select_related_mentions(
    pairs: Iterable[tuple[MentionKey, RelatedMention]],
) -> dict[MentionKey, tuple[RelatedMention, ...]]:
    """(anchor, related) ペアを anchor ごとに束ね、共起記事数降順 top N を返す。

    入力順序の前提を持たず、
    grouping は本関数の責務 (呼び出し側は Row 詰め替えと不正行 skip まで)。
    sort key (``-shared_article_count``, ``name.match_key``) が同値のペアは
    安定 sort により入力順を保存する。
    """
    grouped: dict[MentionKey, list[RelatedMention]] = {}
    for anchor, related in pairs:
        grouped.setdefault(anchor, []).append(related)
    return {
        anchor: tuple(
            sorted(
                items,
                key=lambda r: (-r.shared_article_count, r.name.match_key),
            )[:MAX_RELATED_MENTIONS]
        )
        for anchor, items in grouped.items()
    }
