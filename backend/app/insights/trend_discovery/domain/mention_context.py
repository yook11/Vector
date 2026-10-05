"""共起記事数に基づいて一緒に語られた名前を選ぶ。"""

from __future__ import annotations

from collections.abc import Iterable

from app.insights.trend_discovery.domain.trend import (
    MAX_CO_MENTIONS,
    CoMention,
    MentionKey,
)


def select_co_mentions(
    pairs: Iterable[tuple[MentionKey, CoMention]],
) -> dict[MentionKey, tuple[CoMention, ...]]:
    """(anchor, co-mention) ペアを anchor ごとに束ね、共起記事数降順 top N を返す。

    入力順序の前提を持たず、
    grouping は本関数の責務 (呼び出し側は Row 詰め替えと不正行 skip まで)。
    sort key (``-shared_article_count``, ``name.match_key``) が同値のペアは
    安定 sort により入力順を保存する。
    """
    grouped: dict[MentionKey, list[CoMention]] = {}
    for anchor, co_mention in pairs:
        grouped.setdefault(anchor, []).append(co_mention)
    return {
        anchor: tuple(
            sorted(
                items,
                key=lambda c: (-c.shared_article_count, c.name.match_key),
            )[:MAX_CO_MENTIONS]
        )
        for anchor, items in grouped.items()
    }
