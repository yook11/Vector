"""StubQueryEmbedder の不変条件ユニットテスト (Search BC CI 専用 stub)。

Stage 5 ``StubEmbedder`` と実装は似ているが、解いている問題が違うため独立。
本テストは ``embed_query(str) -> list[float]`` のみを検証する。
"""

from __future__ import annotations

import math

import pytest

from app.search.embedding.stub import StubQueryEmbedder


@pytest.mark.asyncio
async def test_stub_query_embedder_returns_768_dim_vector() -> None:
    embedder = StubQueryEmbedder()
    vector = await embedder.embed_query("test query")
    assert isinstance(vector, list)
    assert len(vector) == 768


@pytest.mark.asyncio
async def test_stub_query_embedder_is_deterministic_per_text() -> None:
    embedder = StubQueryEmbedder()
    v1 = await embedder.embed_query("test query")
    v2 = await embedder.embed_query("test query")
    assert v1 == v2


@pytest.mark.asyncio
async def test_stub_query_embedder_differs_per_text() -> None:
    embedder = StubQueryEmbedder()
    v1 = await embedder.embed_query("first")
    v2 = await embedder.embed_query("second")
    assert v1 != v2


@pytest.mark.asyncio
async def test_stub_query_embedder_returns_unit_norm_vector() -> None:
    embedder = StubQueryEmbedder()
    vector = await embedder.embed_query("anything")
    norm = math.sqrt(sum(v * v for v in vector))
    assert math.isclose(norm, 1.0, abs_tol=1e-6)
