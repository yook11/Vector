"""関連メンション選定 のテスト。"""

from __future__ import annotations

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_context import (
    select_related_mentions,
)
from app.insights.trend_discovery.domain.mention_name import MentionName
from app.insights.trend_discovery.domain.trend import (
    MAX_RELATED_MENTIONS,
    MIN_SHARED_ARTICLES,
    RelatedMention,
)

# テストデータ構築用ヘルパー

_KEY: tuple[str, str] = ("nvidia", "company")
_KEY2: tuple[str, str] = ("openai", "company")


def _related(name: str, count: int) -> RelatedMention:
    return RelatedMention(
        name=MentionName(name),
        type=MentionType.COMPANY,
        shared_article_count=count,
    )


class TestSelectRelatedMentions:
    """select_related_mentions の tie-break・グルーピング・truncate の不変条件。"""

    def test_tie_break_by_match_key_when_shared_count_equal(self) -> None:
        """shared_article_count 同値のとき name.match_key 昇順で tie-break。"""
        pairs = [
            (_KEY, _related("Zebra", MIN_SHARED_ARTICLES)),
            (_KEY, _related("Apple", MIN_SHARED_ARTICLES)),
        ]
        result = select_related_mentions(pairs)
        names = [r.name.match_key for r in result[_KEY]]
        assert names == sorted(names)

    def test_each_anchor_gets_only_its_related(self) -> None:
        """複数 anchor が混在した入力で、各 anchor に正しい related のみが束なる。"""
        pairs = [
            (_KEY, _related("OpenAI", MIN_SHARED_ARTICLES)),
            (_KEY2, _related("Google", MIN_SHARED_ARTICLES)),
            (_KEY, _related("AMD", MIN_SHARED_ARTICLES)),
        ]
        result = select_related_mentions(pairs)
        key1_names = {r.name.match_key for r in result[_KEY]}
        key2_names = {r.name.match_key for r in result[_KEY2]}
        assert key1_names == {"openai", "amd"}
        assert key2_names == {"google"}

    def test_truncates_to_max_related_mentions(self) -> None:
        """MAX_RELATED_MENTIONS + 1 件の related は count 降順上位 3 件に truncate。"""
        pairs = [
            (_KEY, _related(f"peer{i}", MIN_SHARED_ARTICLES + i))
            for i in range(MAX_RELATED_MENTIONS + 1)
        ]
        result = select_related_mentions(pairs)
        assert len(result[_KEY]) == MAX_RELATED_MENTIONS

    def test_truncation_keeps_highest_count(self) -> None:
        """truncate 後は shared_article_count 上位 MAX_RELATED_MENTIONS 件が残る。"""
        # count: 5, 4, 3, 2 → top3 = 5, 4, 3
        pairs = [
            (_KEY, _related("peer_a", MIN_SHARED_ARTICLES + 3)),  # count=5
            (_KEY, _related("peer_b", MIN_SHARED_ARTICLES + 2)),  # count=4
            (_KEY, _related("peer_c", MIN_SHARED_ARTICLES + 1)),  # count=3
            (_KEY, _related("peer_d", MIN_SHARED_ARTICLES)),  # count=2 → 除外
        ]
        result = select_related_mentions(pairs)
        counts = {r.shared_article_count for r in result[_KEY]}
        assert MIN_SHARED_ARTICLES not in counts  # count=2 は除外

    def test_empty_iterable_returns_empty_dict(self) -> None:
        """空 iterable は空 dict を返す。"""
        assert select_related_mentions([]) == {}
