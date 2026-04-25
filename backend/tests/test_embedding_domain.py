"""embedding ドメイン層のユニットテスト (DB 不要)。

EmbeddingVector の次元・有限性・サニティ範囲、EmbeddingDraft.from_inference、
Embedding.from_draft / __post_init__ の不変条件を検証する。
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.analysis.embedding.domain.embedding import Embedding, EmbeddingDraft
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)


def _vec(value: float = 0.1) -> tuple[float, ...]:
    """テスト用の有効な 768 次元ベクトルを生成する。"""
    return tuple([value] * EMBEDDING_DIMENSION)


# ---------------------------------------------------------------------------
# EmbeddingVector — dimension
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# EmbeddingVector — finite & sanity range
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# EmbeddingVector — coercion & immutability
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# EmbeddingDraft.from_inference
# ---------------------------------------------------------------------------


class TestEmbeddingDraftFromInference:
    def test_converts_list_to_vector_vo(self) -> None:
        raw = [0.1] * EMBEDDING_DIMENSION
        draft = EmbeddingDraft.from_inference(vector=raw)
        assert isinstance(draft.vector, EmbeddingVector)
        assert draft.vector.to_list() == raw

    def test_rejects_wrong_dimension(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingDraft.from_inference(vector=[0.1] * (EMBEDDING_DIMENSION - 1))

    def test_rejects_nan(self) -> None:
        raw = [0.1] * EMBEDDING_DIMENSION
        raw[0] = math.nan
        with pytest.raises(ValidationError):
            EmbeddingDraft.from_inference(vector=raw)

    def test_frozen_vector_assignment_rejected(self) -> None:
        draft = EmbeddingDraft.from_inference(vector=[0.1] * EMBEDDING_DIMENSION)
        with pytest.raises(ValidationError):
            draft.vector = EmbeddingVector(root=_vec(0.2))  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Embedding.from_draft / __post_init__
# ---------------------------------------------------------------------------


class TestEmbeddingFromDraft:
    def test_synthesizes_identity_and_model_name(self) -> None:
        draft = EmbeddingDraft.from_inference(vector=[0.1] * EMBEDDING_DIMENSION)
        embedding = Embedding.from_draft(
            draft, analysis_id=42, model_name="cl-nagoya/ruri-v3-310m"
        )
        assert embedding.analysis_id == 42
        assert embedding.model_name == "cl-nagoya/ruri-v3-310m"
        assert embedding.vector is draft.vector

    def test_rejects_zero_analysis_id(self) -> None:
        draft = EmbeddingDraft.from_inference(vector=[0.1] * EMBEDDING_DIMENSION)
        with pytest.raises(ValueError, match="analysis_id must be positive"):
            Embedding.from_draft(draft, analysis_id=0, model_name="m")

    def test_rejects_negative_analysis_id(self) -> None:
        draft = EmbeddingDraft.from_inference(vector=[0.1] * EMBEDDING_DIMENSION)
        with pytest.raises(ValueError, match="analysis_id must be positive"):
            Embedding.from_draft(draft, analysis_id=-1, model_name="m")

    def test_rejects_empty_model_name(self) -> None:
        draft = EmbeddingDraft.from_inference(vector=[0.1] * EMBEDDING_DIMENSION)
        with pytest.raises(ValueError, match="model_name must be non-empty"):
            Embedding.from_draft(draft, analysis_id=1, model_name="")

    def test_rejects_model_name_over_100_chars(self) -> None:
        draft = EmbeddingDraft.from_inference(vector=[0.1] * EMBEDDING_DIMENSION)
        with pytest.raises(ValueError, match="at most 100 chars"):
            Embedding.from_draft(draft, analysis_id=1, model_name="x" * 101)

    def test_accepts_model_name_at_100_chars(self) -> None:
        draft = EmbeddingDraft.from_inference(vector=[0.1] * EMBEDDING_DIMENSION)
        Embedding.from_draft(draft, analysis_id=1, model_name="x" * 100)


class TestEmbeddingFrozen:
    def test_frozen_dataclass_assignment_rejected(self) -> None:
        draft = EmbeddingDraft.from_inference(vector=[0.1] * EMBEDDING_DIMENSION)
        embedding = Embedding.from_draft(draft, analysis_id=1, model_name="m")
        with pytest.raises(AttributeError):
            embedding.analysis_id = 2  # type: ignore[misc]
