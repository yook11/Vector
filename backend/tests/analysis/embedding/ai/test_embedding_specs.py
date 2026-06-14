"""``GEMINI_EMBEDDING_SPEC`` の構造を固定する golden table テスト。

Stage 5 (embedding) は prompt template / response schema を持たないため、
Stage 3/4 と異なり ``version`` / ``compute_call_signature`` の field は
``EmbeddingCallSpec`` に存在しない。代わりに ``dimension`` /
``output_dimensionality`` / ``task_type`` / ``document_prefix`` が SSoT 値として
固定される。

``dimension`` と ``output_dimensionality`` は責務が違うため別 field として
持つ (前者は VO / DB ``HALFVEC(768)`` 契約値、後者は SDK へ渡す API config 値)。
運用上両者は一致するため、横断 invariant として等値を担保する。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pgvector.sqlalchemy import HALFVEC

from app.analysis.embedding.ai.spec import GEMINI_EMBEDDING_SPEC
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.models.analyzed_article_record import AnalyzedArticleRecord


def test_gemini_provider_is_gemini() -> None:
    assert GEMINI_EMBEDDING_SPEC.provider == "gemini"


def test_gemini_model_is_embedding_001() -> None:
    assert GEMINI_EMBEDDING_SPEC.model == "gemini-embedding-001"


def test_gemini_dimension_equals_embedding_dimension() -> None:
    """DB ``HALFVEC`` 列 + ``EmbeddingVector`` VO の契約値 = SSoT。"""
    assert GEMINI_EMBEDDING_SPEC.dimension == EMBEDDING_DIMENSION


def test_gemini_output_dimensionality_equals_embedding_dimension() -> None:
    """SDK ``EmbedContentConfig`` へ渡す API config 値 = SSoT。"""
    assert GEMINI_EMBEDDING_SPEC.output_dimensionality == EMBEDDING_DIMENSION


def test_gemini_task_type_is_retrieval_document() -> None:
    """Stage 5 は document 永続化専用 (Search BC が RETRIEVAL_QUERY を担当)。"""
    assert GEMINI_EMBEDDING_SPEC.task_type == "RETRIEVAL_DOCUMENT"


def test_gemini_document_prefix_is_empty() -> None:
    """Gemini embedding は prefix を必要としない。"""
    assert GEMINI_EMBEDDING_SPEC.document_prefix == ""


def test_gemini_rate_limit_policy_equals_provider_model_with_no_rules() -> None:
    """Gemini embedding API の RPM/RPD は tier 依存で確定値なし。"""
    assert GEMINI_EMBEDDING_SPEC.rate_limit_policy == AIModelRateLimitPolicy(
        provider="gemini",
        model="gemini-embedding-001",
        rules=(),
    )


def test_gemini_spec_is_frozen() -> None:
    """``frozen=True`` + ``slots=True`` で field 代入は ``FrozenInstanceError``。"""
    with pytest.raises(FrozenInstanceError):
        GEMINI_EMBEDDING_SPEC.provider = "openai"  # type: ignore[misc]


# 横断 invariant


def test_dimension_equals_output_dimensionality() -> None:
    """責務は別だが運用上は一致する契約を pin する。

    ``dimension`` は VO / DB の契約値、``output_dimensionality`` は SDK config 値。
    両者が乖離すると SDK の戻り値が VO 制約を満たさず boundary で raise する。
    """
    spec = GEMINI_EMBEDDING_SPEC
    assert spec.dimension == spec.output_dimensionality


def test_orm_embedding_column_dim_equals_embedding_dimension() -> None:
    """ORM ``HALFVEC`` literal と SSoT の一致を保証し、永続化契約の鎖を閉じる。

    ``app.models`` は ``embedding.domain`` に依存しないため次元は literal のままだが、
    本テストが ``EMBEDDING_DIMENSION`` との乖離を構造的に検出する。
    """
    column_type = AnalyzedArticleRecord.__table__.c.embedding.type
    assert isinstance(column_type, HALFVEC)
    assert column_type.dim == EMBEDDING_DIMENSION
