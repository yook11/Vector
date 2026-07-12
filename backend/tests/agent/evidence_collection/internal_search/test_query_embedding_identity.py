"""query_hash_of と embedder_identity_of の unit テスト (DB 不要)。"""

from __future__ import annotations

import dataclasses

import pytest

from app.agent.evidence_collection.internal_search.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
    QueryEmbeddingCallSpec,
    embedder_identity_of,
)
from app.agent.evidence_collection.internal_search.query_embedding import query_hash_of

_SEPARATOR = ":"


def _spec_with_model(model: str) -> QueryEmbeddingCallSpec:
    """GEMINI_QUERY_EMBEDDING_SPEC から model だけ差し替えた spec を返す。"""
    return dataclasses.replace(GEMINI_QUERY_EMBEDDING_SPEC, model=model)


class TestEmbedderIdentityOf:
    def test_identity_contains_all_spec_fields_joined_by_colon(self) -> None:
        """identity は spec 5 フィールドを ':' 順で連結する。"""
        spec = GEMINI_QUERY_EMBEDDING_SPEC
        # 各フィールドから期待値を組み立てる (完成文字列を直書きしない)。
        expected = _SEPARATOR.join(
            [
                spec.provider,
                spec.model,
                spec.task_type,
                str(spec.dimension),
                str(spec.output_dimensionality),
            ]
        )

        assert embedder_identity_of(spec) == expected

    def test_different_model_produces_different_identity(self) -> None:
        """model が異なる spec は別 identity になる。"""
        spec_a = GEMINI_QUERY_EMBEDDING_SPEC
        spec_b = _spec_with_model("gemini-embedding-002")

        assert embedder_identity_of(spec_a) != embedder_identity_of(spec_b)

    def test_colon_in_model_raises_value_error(self) -> None:
        """構成要素 (model) に ':' を含む spec は ValueError を送出する。"""
        spec_with_colon = _spec_with_model("gemini:invalid:model")

        with pytest.raises(ValueError, match=":"):
            embedder_identity_of(spec_with_colon)

    def test_colon_in_provider_raises_value_error(self) -> None:
        """provider に ':' を含む spec でも ValueError を送出する。"""
        spec_with_colon = dataclasses.replace(
            GEMINI_QUERY_EMBEDDING_SPEC,
            provider="bad:provider",
        )

        with pytest.raises(ValueError):
            embedder_identity_of(spec_with_colon)

    def test_colon_in_task_type_raises_value_error(self) -> None:
        """task_type に ':' を含む spec でも ValueError を送出する。"""
        spec_with_colon = dataclasses.replace(
            GEMINI_QUERY_EMBEDDING_SPEC,
            task_type="RETRIEVAL:QUERY",
        )

        with pytest.raises(ValueError):
            embedder_identity_of(spec_with_colon)


class TestQueryHashOf:
    def test_deterministic_same_input_same_hash(self) -> None:
        """同一入力は常に同一 hash を返す。"""
        text = "NVIDIA earnings report"

        assert query_hash_of(text) == query_hash_of(text)

    def test_hash_is_hex_string_of_length_64(self) -> None:
        """hash は hex 文字種・長さ 64 の文字列 (sha256 hex digest の仕様)。"""
        result = query_hash_of("any input text")

        assert len(result) == 64  # sha256 hex = 256bit / 4bit per char
        assert all(c in "0123456789abcdef" for c in result)

    def test_different_inputs_produce_different_hashes(self) -> None:
        """入力が異なれば hash も異なる。"""
        assert query_hash_of("query A") != query_hash_of("query B")

    def test_no_renormalization_trailing_space_differs(self) -> None:
        """末尾空白を含む入力と含まない入力は別 hash になる (再正規化しない)。"""
        assert query_hash_of("q ") != query_hash_of("q")

    def test_no_renormalization_leading_space_differs(self) -> None:
        """先頭空白を含む入力と含まない入力も別 hash になる (再正規化しない)。"""
        assert query_hash_of(" q") != query_hash_of("q")
