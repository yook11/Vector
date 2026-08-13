"""Gemini query embedding spec tests."""

from __future__ import annotations

from app.agent.evidence_collection.internal_search.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
)


def test_gemini_query_spec_matches_external_api_contract() -> None:
    # provider / model / task_type は Gemini API との外部契約値。
    # 誤って変更すると埋め込み空間が変わり cache を汚染する。
    assert GEMINI_QUERY_EMBEDDING_SPEC.provider == "gemini"
    assert GEMINI_QUERY_EMBEDDING_SPEC.model == "gemini-embedding-001"
    assert GEMINI_QUERY_EMBEDDING_SPEC.task_type == "RETRIEVAL_QUERY"
