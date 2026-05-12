"""embed_search_query の cache hit / miss と quota 消費の構造的不変条件 (unit)。

red-team C1 対策の重要な振る舞いを mock のみで検証する (DB 不要、unit マーカー)。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.search.quota import SearchQuotaExceededError
from app.search.service import embed_search_query


@pytest.mark.asyncio
async def test_embed_search_query_does_not_consume_quota_on_cache_hit() -> None:
    """cache hit のときは quota を消費しない (eval が呼ばれないことで証明)。

    キャッシュ活用を促す設計: 攻撃者は q=$RANDOM で常に miss するため、
    正規利用 (再検索や同一クエリ) を quota 消費から外しても DoS には効かない。
    """
    redis = MagicMock()
    redis.eval = AsyncMock()
    fake_vector = [0.1] * 768
    unused_embedder = MagicMock()
    unused_embedder.embed_query = AsyncMock()
    with patch(
        "app.search.embedding_cache.get_query_embedding",
        new_callable=AsyncMock,
        return_value=fake_vector,
    ):
        result = await embed_search_query(
            "ai",
            user_id=uuid.uuid4(),
            redis=redis,
            daily_max=100,
            embedder=unused_embedder,
        )
    assert result == fake_vector
    redis.eval.assert_not_called()
    unused_embedder.embed_query.assert_not_called()


@pytest.mark.asyncio
async def test_embed_search_query_consumes_quota_on_cache_miss() -> None:
    """cache miss のときに 1 回 quota を消費し embedder を呼出 + cache 書き戻す。"""
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=1)
    fake_vector = [0.2] * 768
    fake_embedder = MagicMock()
    fake_embedder.embed_query = AsyncMock(return_value=fake_vector)
    with (
        patch(
            "app.search.embedding_cache.get_query_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.search.embedding_cache.set_query_embedding",
            new_callable=AsyncMock,
        ) as set_cache,
    ):
        result = await embed_search_query(
            "ai",
            user_id=uuid.uuid4(),
            redis=redis,
            daily_max=100,
            embedder=fake_embedder,
        )
    assert result == fake_vector
    redis.eval.assert_called_once()
    fake_embedder.embed_query.assert_awaited_once_with("ai")
    set_cache.assert_awaited_once()


@pytest.mark.asyncio
async def test_embed_search_query_propagates_quota_exhausted_on_miss() -> None:
    """cache miss + Lua=0 → SearchQuotaExceededError 伝播 + embedder 呼ばれず。

    embedder 呼出より前で fail-fast することで、Gemini API への課金経路と
    Better Auth pg.Pool への並行呼出を上流で塞ぐ (red-team C1 構造防御)。
    """
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=0)
    fake_embedder = MagicMock()
    fake_embedder.embed_query = AsyncMock()
    with patch(
        "app.search.embedding_cache.get_query_embedding",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(SearchQuotaExceededError):
            await embed_search_query(
                "ai",
                user_id=uuid.uuid4(),
                redis=redis,
                daily_max=100,
                embedder=fake_embedder,
            )
    fake_embedder.embed_query.assert_not_called()
