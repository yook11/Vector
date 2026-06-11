"""embedding ドメイン層のユニットテスト (DB 不要)。

``EmbeddingVector`` VO の次元・有限性・サニティ範囲・frozen を検証する。
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)


def _vec(value: float = 0.1) -> tuple[float, ...]:
    """テスト用の有効な 768 次元ベクトルを生成する。"""
    return tuple([value] * EMBEDDING_DIMENSION)


class TestEmbeddingVectorDimension:
    def test_accepts_exactly_768_dimensions(self) -> None:
        vec = EmbeddingVector(root=_vec())
        assert len(vec) == EMBEDDING_DIMENSION

    def test_rejects_767_dimensions(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingVector(root=tuple([0.1] * (EMBEDDING_DIMENSION - 1)))

    def test_rejects_769_dimensions(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingVector(root=tuple([0.1] * (EMBEDDING_DIMENSION + 1)))

    def test_rejects_empty_vector(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingVector(root=())


class TestEmbeddingVectorFinite:
    def test_rejects_nan(self) -> None:
        v = list(_vec())
        v[0] = math.nan
        with pytest.raises(ValidationError):
            EmbeddingVector(root=tuple(v))

    def test_rejects_positive_infinity(self) -> None:
        v = list(_vec())
        v[10] = math.inf
        with pytest.raises(ValidationError):
            EmbeddingVector(root=tuple(v))

    def test_rejects_negative_infinity(self) -> None:
        v = list(_vec())
        v[10] = -math.inf
        with pytest.raises(ValidationError):
            EmbeddingVector(root=tuple(v))


class TestEmbeddingVectorSanityRange:
    def test_rejects_value_above_upper_bound(self) -> None:
        v = list(_vec())
        v[0] = 1e5
        with pytest.raises(ValidationError):
            EmbeddingVector(root=tuple(v))

    def test_rejects_value_below_lower_bound(self) -> None:
        v = list(_vec())
        v[0] = -1e5
        with pytest.raises(ValidationError):
            EmbeddingVector(root=tuple(v))

    def test_accepts_typical_normalized_range(self) -> None:
        """正規化済み埋め込みは [-1, 1] 程度に収まる典型値を通す。"""
        v = list(_vec(0.5))
        v[0] = -0.99
        v[1] = 0.99
        EmbeddingVector(root=tuple(v))


class TestEmbeddingVectorCoercion:
    def test_coerces_list_to_tuple(self) -> None:
        vec = EmbeddingVector(root=[0.1] * EMBEDDING_DIMENSION)  # type: ignore[arg-type]
        assert isinstance(vec.root, tuple)

    def test_to_list_round_trip(self) -> None:
        original = [0.5] * EMBEDDING_DIMENSION
        vec = EmbeddingVector(root=tuple(original))
        assert vec.to_list() == original


class TestEmbeddingVectorFrozen:
    def test_frozen_root_assignment_rejected(self) -> None:
        vec = EmbeddingVector(root=_vec())
        with pytest.raises(ValidationError):
            vec.root = _vec(0.2)  # type: ignore[misc]
