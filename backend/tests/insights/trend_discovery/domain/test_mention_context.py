"""mention_context (key_point / related mention の文脈選定純関数) のテスト。"""

from __future__ import annotations

import pytest

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_context import (
    KeyPointCandidate,
    _cosine_distance,
    select_key_points,
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


# 直交する単位ベクトル (コサイン距離 = 1.0)
_VEC_X = [1.0, 0.0, 0.0]
_VEC_Y = [0.0, 1.0, 0.0]

# 同一ベクトル (コサイン距離 = 0.0)
_VEC_A = [1.0, 2.0, 3.0]


class TestSelectKeyPoints:
    """select_key_points の採択ポリシーの不変条件。"""

    def test_content_none_skips_without_consuming_assessment_budget(self) -> None:
        """content=None の候補は analyzed_article_id を消費せず、同 assessment の
        次候補が採択される。"""
        candidates = [
            KeyPointCandidate(analyzed_article_id=1, embedding=None, content=None),
            KeyPointCandidate(analyzed_article_id=1, embedding=None, content="valid"),
        ]
        result = select_key_points({_KEY: candidates})
        assert result[_KEY] == ("valid",)

    def test_proximity_checked_against_all_accepted_vectors(self) -> None:
        """採択済み「全」ベクトルと比較される: 2番目採択 B に近い C は畳まれる。

        構成: A (採択) → B (A から遠い, 採択) → C (B に近い, 畳まれる)
        C は A から遠くても、B が accepted_vectors に追加された後で比較されるため除外。
        """
        # A = [1,0,0], B = [0,1,0] は直交 (距離 1.0 > 0.1 → A採択後 B も採択)
        # C = [0, 1, 0.001] は B にほぼ同一 (距離 << 0.1 → 畳まれる)
        vec_c_near_b = [0.0, 1.0, 0.001]
        candidates = [
            KeyPointCandidate(
                analyzed_article_id=1, embedding=_VEC_X, content="point_a"
            ),
            KeyPointCandidate(
                analyzed_article_id=2, embedding=_VEC_Y, content="point_b"
            ),
            KeyPointCandidate(
                analyzed_article_id=3, embedding=vec_c_near_b, content="point_c"
            ),
        ]
        result = select_key_points({_KEY: candidates})
        # MAX_KEY_POINTS_PER_MENTION=2 なので A,B が採択され C は畳まれる
        assert "point_a" in result[_KEY]
        assert "point_b" in result[_KEY]
        assert "point_c" not in result[_KEY]

    def test_distance_strictly_less_than_threshold_is_deduped(self) -> None:
        """コサイン距離が KEY_POINT_DEDUP_DISTANCE より明確に小さいペアは畳まれる。"""
        # [1,0,0] と [1, 0.001, 0] は距離が 0.1 より十分小さい
        vec_almost_same = [1.0, 0.001, 0.0]
        candidates = [
            KeyPointCandidate(analyzed_article_id=1, embedding=_VEC_X, content="first"),
            KeyPointCandidate(
                analyzed_article_id=2, embedding=vec_almost_same, content="dup"
            ),
        ]
        result = select_key_points({_KEY: candidates})
        assert result[_KEY] == ("first",)

    def test_distance_clearly_above_threshold_both_accepted(self) -> None:
        """コサイン距離が KEY_POINT_DEDUP_DISTANCE より明確に大きいペアは両方採択。"""
        # 直交ベクトルの距離 = 1.0 >> KEY_POINT_DEDUP_DISTANCE(0.1)
        candidates = [
            KeyPointCandidate(analyzed_article_id=1, embedding=_VEC_X, content="first"),
            KeyPointCandidate(
                analyzed_article_id=2, embedding=_VEC_Y, content="second"
            ),
        ]
        result = select_key_points({_KEY: candidates})
        assert "first" in result[_KEY]
        assert "second" in result[_KEY]

    def test_zero_vectors_distance_is_one_so_both_accepted(self) -> None:
        """ゼロベクトル同士は距離 1.0 扱いとなり、dedup の対象にならない。"""
        zero = [0.0, 0.0, 0.0]
        candidates = [
            KeyPointCandidate(analyzed_article_id=1, embedding=zero, content="first"),
            KeyPointCandidate(analyzed_article_id=2, embedding=zero, content="second"),
        ]
        result = select_key_points({_KEY: candidates})
        assert "first" in result[_KEY]
        assert "second" in result[_KEY]

    def test_embedding_none_does_not_enter_accepted_vectors(self) -> None:
        """embedding=None の採択候補は accepted_vectors に入らない。
        同一ベクトルを持つ後続候補が畳まれないことで確認する。"""
        # embedding=None の候補が採択されても accepted_vectors が空のまま →
        # 後続の [1,0,0] は比較対象なしで採択される
        candidates = [
            KeyPointCandidate(analyzed_article_id=1, embedding=None, content="no_vec"),
            KeyPointCandidate(
                analyzed_article_id=2, embedding=_VEC_X, content="with_vec"
            ),
        ]
        result = select_key_points({_KEY: candidates})
        assert "no_vec" in result[_KEY]
        assert "with_vec" in result[_KEY]

    def test_all_content_none_produces_empty_tuple_for_key(self) -> None:
        """全候補 content=None のとき、そのキーは空 tuple で結果に現れる。"""
        candidates = [
            KeyPointCandidate(analyzed_article_id=1, embedding=None, content=None),
            KeyPointCandidate(analyzed_article_id=2, embedding=None, content=None),
        ]
        result = select_key_points({_KEY: candidates})
        assert _KEY in result
        assert result[_KEY] == ()

    def test_empty_mapping_returns_empty_dict(self) -> None:
        """空 Mapping は空 dict を返す。"""
        assert select_key_points({}) == {}

    def test_second_candidate_from_same_assessment_skipped(self) -> None:
        """同一 analyzed_article_id の 2 本目は skip される。"""
        candidates = [
            KeyPointCandidate(analyzed_article_id=1, embedding=_VEC_X, content="first"),
            # 同 analyzed_article_id, 別 content, 遠いベクトル → skip
            KeyPointCandidate(
                analyzed_article_id=1, embedding=_VEC_Y, content="second"
            ),
        ]
        result = select_key_points({_KEY: candidates})
        assert result[_KEY] == ("first",)


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


class TestCosineDistance:
    """_cosine_distance の幾何的不変条件。"""

    def test_identical_vector_distance_is_zero(self) -> None:
        """同一ベクトル同士のコサイン距離は 0.0。"""
        assert _cosine_distance(_VEC_A, _VEC_A) == pytest.approx(0.0)

    def test_orthogonal_vectors_distance_is_one(self) -> None:
        """直交ベクトルのコサイン距離は 1.0。"""
        assert _cosine_distance(_VEC_X, _VEC_Y) == pytest.approx(1.0)

    def test_zero_vector_distance_is_one(self) -> None:
        """ゼロベクトルは最大距離 1.0 扱い (除算回避)。"""
        zero = [0.0, 0.0, 0.0]
        assert _cosine_distance(zero, _VEC_A) == 1.0
        assert _cosine_distance(_VEC_A, zero) == 1.0
        assert _cosine_distance(zero, zero) == 1.0
