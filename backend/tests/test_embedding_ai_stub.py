"""StubEmbedder の不変条件ユニットテスト (CI 専用 stub)。"""

from __future__ import annotations

import math

import pytest

from app.analysis.embedding.ai.stub import StubEmbedder


@pytest.mark.asyncio
async def test_stub_embedder_returns_768_dim_vector() -> None:
    embedder = StubEmbedder()
    vector = await embedder.embed_query("test query")
    assert len(vector) == 768


@pytest.mark.asyncio
async def test_stub_embedder_is_deterministic_per_text() -> None:
    embedder = StubEmbedder()
    v1 = await embedder.embed_query("test query")
    v2 = await embedder.embed_query("test query")
    assert v1 == v2


@pytest.mark.asyncio
async def test_stub_embedder_differs_per_text() -> None:
    embedder = StubEmbedder()
    v1 = await embedder.embed_query("first")
    v2 = await embedder.embed_query("second")
    assert v1 != v2


@pytest.mark.asyncio
async def test_stub_embedder_returns_unit_norm_vector() -> None:
    embedder = StubEmbedder()
    vector = await embedder.embed_query("anything")
    norm = math.sqrt(sum(v * v for v in vector))
    assert math.isclose(norm, 1.0, abs_tol=1e-6)
