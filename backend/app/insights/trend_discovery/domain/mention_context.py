"""mention の文脈選定ポリシー (key_point / related mention)。

repository が SQL で取得した素材から「どれを snapshot に載せるか」を決める
純関数群。SQL 構築・Row 詰め替え・不正行の skip + warning は repository の
責務で、本モジュールは I/O も logger も持たない。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

from app.insights.trend_discovery.domain.trend import (
    KEY_POINT_DEDUP_DISTANCE,
    MAX_KEY_POINTS_PER_MENTION,
    MAX_RELATED_MENTIONS,
    MentionKey,
    RelatedMention,
)


class KeyPointCandidate(NamedTuple):
    """key_point content の採択候補 1 行分 (repository が Row から詰め替える)。"""

    analyzed_article_id: int
    embedding: list[float] | None
    content: str | None


def select_key_points(
    candidates_by_mention: Mapping[MentionKey, Sequence[KeyPointCandidate]],
) -> dict[MentionKey, tuple[str, ...]]:
    """mention ごとに key_point content を記事レベル dedup して最大 N 本選ぶ。

    候補列は recency 降順 (analyzed_at DESC, id DESC) が前提で、並べ替えずに
    先頭から走査するため、この順序がそのまま最新優先の採択優先度になる。
    grouping とこの順序の保証は呼び出し側 = repository の責務 (SQL の ORDER BY
    が決めた順序を保持したまま束ねる必要があるため)。順序が崩れても例外は
    出ず、無言で古い key_point を採択するので、供給側の順序を崩さないこと。
    同一 analyzed article からは 1 本まで (content が None の行は採択にも ID
    消費にも数えない)。採択済み全 content との cosine 距離が
    ``KEY_POINT_DEDUP_DISTANCE`` 未満なら同一トピックとして畳む (embedding が
    None の候補は近接判定をスキップし、採択済ベクトル集合にも入らない)。入力に
    現れた全 key は結果にも現れる (採択 0 本なら空 tuple)。
    """
    result: dict[MentionKey, tuple[str, ...]] = {}
    for key, candidates in candidates_by_mention.items():
        contents: list[str] = []
        seen_analyzed_articles: set[int] = set()
        accepted_vectors: list[list[float]] = []
        for candidate in candidates:
            if len(contents) >= MAX_KEY_POINTS_PER_MENTION:
                break
            if (
                candidate.content is None
                or candidate.analyzed_article_id in seen_analyzed_articles
            ):
                continue
            vector = candidate.embedding
            if vector is not None and any(
                _cosine_distance(vector, v) < KEY_POINT_DEDUP_DISTANCE
                for v in accepted_vectors
            ):
                continue
            contents.append(candidate.content)
            seen_analyzed_articles.add(candidate.analyzed_article_id)
            if vector is not None:
                accepted_vectors.append(vector)
        result[key] = tuple(contents)
    return result


def select_related_mentions(
    pairs: Iterable[tuple[MentionKey, RelatedMention]],
) -> dict[MentionKey, tuple[RelatedMention, ...]]:
    """(anchor, related) ペアを anchor ごとに束ね、共起記事数降順 top N を返す。

    内部で sort するため ``select_key_points`` と違い入力順序の前提を持たず、
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


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """cosine 距離 (1 - cosine 類似度)。ゼロベクトルは最大距離扱い。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)
