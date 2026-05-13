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

from app.analysis.embedding.ai.spec import GEMINI_EMBEDDING_SPEC
from app.analysis.rate_policy import RatePolicy

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_gemini_provider_is_gemini() -> None:
    assert GEMINI_EMBEDDING_SPEC.provider == "gemini"


def test_gemini_model_is_embedding_001() -> None:
    assert GEMINI_EMBEDDING_SPEC.model == "gemini-embedding-001"


def test_gemini_dimension_is_768() -> None:
    """DB ``HALFVEC(768)`` 列 + ``EmbeddingVector`` VO の契約値。"""
    assert GEMINI_EMBEDDING_SPEC.dimension == 768


def test_gemini_output_dimensionality_is_768() -> None:
    """SDK ``EmbedContentConfig`` へ渡す API config 値。"""
    assert GEMINI_EMBEDDING_SPEC.output_dimensionality == 768


def test_gemini_task_type_is_retrieval_document() -> None:
    """Stage 5 は document 永続化専用 (Search BC が RETRIEVAL_QUERY を担当)。"""
    assert GEMINI_EMBEDDING_SPEC.task_type == "RETRIEVAL_DOCUMENT"


def test_gemini_document_prefix_is_empty() -> None:
    """Gemini embedding は prefix を必要としない。"""
    assert GEMINI_EMBEDDING_SPEC.document_prefix == ""


def test_gemini_rate_policy_equals_provider_model_with_none_rpm_rpd() -> None:
    """Gemini embedding API の RPM/RPD は tier 依存で確定値なし。"""
    assert GEMINI_EMBEDDING_SPEC.rate_policy == RatePolicy(
        provider="gemini",
        model="gemini-embedding-001",
        rpm=None,
        rpd=None,
    )


def test_gemini_spec_is_frozen() -> None:
    """``frozen=True`` + ``slots=True`` で field 代入は ``FrozenInstanceError``。"""
    with pytest.raises(FrozenInstanceError):
        GEMINI_EMBEDDING_SPEC.provider = "openai"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 横断 invariant
# ---------------------------------------------------------------------------


def test_dimension_equals_output_dimensionality() -> None:
    """責務は別だが運用上は一致する契約を pin する。

    ``dimension`` は VO / DB の契約値、``output_dimensionality`` は SDK config 値。
    両者が乖離すると SDK の戻り値が VO 制約を満たさず boundary で raise する。
    """
    spec = GEMINI_EMBEDDING_SPEC
    assert spec.dimension == spec.output_dimensionality
