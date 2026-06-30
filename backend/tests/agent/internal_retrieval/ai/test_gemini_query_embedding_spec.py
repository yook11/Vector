"""Gemini query embedding spec tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.agent.internal_retrieval.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
)
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.rate_limit import AIModelRateLimitPolicy


def test_gemini_query_provider_is_gemini() -> None:
    assert GEMINI_QUERY_EMBEDDING_SPEC.provider == "gemini"


def test_gemini_query_model_is_embedding_001() -> None:
    assert GEMINI_QUERY_EMBEDDING_SPEC.model == "gemini-embedding-001"


def test_gemini_query_dimension_equals_embedding_dimension() -> None:
    assert GEMINI_QUERY_EMBEDDING_SPEC.dimension == EMBEDDING_DIMENSION


def test_gemini_query_output_dimensionality_equals_embedding_dimension() -> None:
    assert GEMINI_QUERY_EMBEDDING_SPEC.output_dimensionality == EMBEDDING_DIMENSION


def test_gemini_query_task_type_is_retrieval_query() -> None:
    assert GEMINI_QUERY_EMBEDDING_SPEC.task_type == "RETRIEVAL_QUERY"


def test_gemini_query_rate_limit_policy_equals_provider_model_with_no_rules() -> None:
    assert GEMINI_QUERY_EMBEDDING_SPEC.rate_limit_policy == AIModelRateLimitPolicy(
        provider="gemini",
        model="gemini-embedding-001",
        rules=(),
    )


def test_gemini_query_spec_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        GEMINI_QUERY_EMBEDDING_SPEC.provider = "openai"  # type: ignore[misc]
