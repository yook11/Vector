"""StubEmbedder + factory switch のユニットテスト (CI 専用 stub の不変条件)。"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from app.analysis.embedder.factory import get_embedder
from app.analysis.embedder.gemini import GeminiEmbedder
from app.analysis.embedder.stub import StubEmbedder
from app.config import settings


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


def test_factory_returns_stub_when_provider_is_stub() -> None:
    with patch.object(settings, "embedder_provider", "stub"):
        embedder = get_embedder()
    assert isinstance(embedder, StubEmbedder)


def test_factory_rejects_stub_in_production() -> None:
    with (
        patch.object(settings, "embedder_provider", "stub"),
        patch.object(settings, "env", "production"),
        pytest.raises(ValueError, match="stub.*production"),
    ):
        get_embedder()


def test_factory_returns_gemini_when_provider_is_gemini() -> None:
    """gemini provider + 有効な API key で GeminiEmbedder が返ることを確認。"""
    from pydantic import SecretStr

    with (
        patch.object(settings, "embedder_provider", "gemini"),
        patch.object(settings, "gemini_api_key", SecretStr("dummy-but-non-empty")),
    ):
        embedder = get_embedder()
    assert isinstance(embedder, GeminiEmbedder)
