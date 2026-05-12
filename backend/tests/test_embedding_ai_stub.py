"""StubEmbedder の不変条件ユニットテスト (Stage 5 CI 専用 stub)。

Stage 5 BC 分離後、``StubEmbedder`` は ``ReadyForEmbedding`` を受ける document
専用 hierarchy となった。Search BC 用の stub は
``tests/fakes/stub_query_embedder.py::StubQueryEmbedder`` に独立する。
"""

from __future__ import annotations

import math

import pytest

from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from tests.fakes.stub_embedder import StubEmbedder


def _ready(text: str) -> ReadyForEmbedding:
    return ReadyForEmbedding(analysis_id=1, text_for_embedding=text, article_id=1)


@pytest.mark.asyncio
async def test_stub_embedder_returns_768_dim_vector() -> None:
    embedder = StubEmbedder()
    vector = await embedder.embed_document(_ready("test query"))
    assert isinstance(vector, EmbeddingVector)
    assert len(vector.to_list()) == 768


@pytest.mark.asyncio
async def test_stub_embedder_is_deterministic_per_text() -> None:
    embedder = StubEmbedder()
    v1 = await embedder.embed_document(_ready("test query"))
    v2 = await embedder.embed_document(_ready("test query"))
    assert v1.to_list() == v2.to_list()


@pytest.mark.asyncio
async def test_stub_embedder_differs_per_text() -> None:
    embedder = StubEmbedder()
    v1 = await embedder.embed_document(_ready("first"))
    v2 = await embedder.embed_document(_ready("second"))
    assert v1.to_list() != v2.to_list()


@pytest.mark.asyncio
async def test_stub_embedder_returns_unit_norm_vector() -> None:
    embedder = StubEmbedder()
    vector = await embedder.embed_document(_ready("anything"))
    norm = math.sqrt(sum(v * v for v in vector.to_list()))
    assert math.isclose(norm, 1.0, abs_tol=1e-6)
