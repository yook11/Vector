"""セマンティック検索サービス — embedding ベースの分析的探索。"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as aioredis

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedder.factory import get_embedder
from app.analysis.errors import AnalysisDomainError
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.search.errors import SearchError
from app.search.quota import consume_search_quota
from app.search.repository import SemanticSearchRepository
from app.services.articles import build_brief


async def embed_search_query(
    text: str,
    *,
    user_id: UUID,
    redis: aioredis.Redis,
    daily_max: int,
    embedder: BaseEmbedder | None = None,
) -> list[float]:
    """RETRIEVAL_QUERY タスクタイプで検索クエリを embedding 化する。

    まず Redis embedding キャッシュを確認し、miss 時のみ:
      1. per-user 日次クォータを atomic に消費 (上限超過なら 429 経路へ)
      2. embedder を呼んで結果をキャッシュへ書き戻す

    cache hit ではクォータを消費しない (キャッシュ活用を促す。攻撃者は q=$RANDOM
    で常に miss するため効果は同じ)。embedding キャッシュ障害時は
    ``get_query_embedding`` が None を返し、結果として「miss 扱い → quota 消費 +
    直 API 呼出」へグレースフルに降格する。

    Args:
        text: Search query text (expected to be pre-normalized by the caller).
        user_id: BFF JWT の sub。クォータ消費の主体。
        redis: 共有 Redis クライアント。
        daily_max: ユーザー 1 人 1 日あたりの上限。
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        A list of floats representing the query embedding.

    Raises:
        SearchQuotaExceededError: cache miss 時にユーザーが当日のクォータを使い切った。
        SearchError: If the API call fails.
    """
    from app.search.embedding_cache import get_query_embedding, set_query_embedding

    cached = await get_query_embedding(text)
    if cached is not None:
        return cached

    # quota 消費は embedder 呼出の **直前**。Lua atomic なので race-free。
    # 例外 (SearchQuotaExceededError / RedisError) はそのまま伝播し、
    # exception_handler が 429 / 500 にマップする。
    # 「embedder が落ちたら quota を戻す」は意図的にやらない (攻撃者がエラー誘発で
    # quota 回避する経路を消すため、消費 = API call attempt と定義)。
    await consume_search_quota(redis, user_id, requested=1, daily_max=daily_max)

    if embedder is None:
        embedder = get_embedder()

    try:
        vector = await embedder.embed_query(text)
    except AnalysisDomainError as e:
        raise SearchError(str(e)) from e

    await set_query_embedding(text, vector)
    return vector


class SemanticSearchService:
    def __init__(self, search_repo: SemanticSearchRepository) -> None:
        self.search_repo = search_repo

    async def search(
        self,
        query: SemanticSearchParams,
        *,
        user_id: UUID,
        redis: aioredis.Redis,
        daily_max: int,
    ) -> PaginatedArticleResponse:
        """ユーザーのクエリテキストとのセマンティック類似度で記事を検索する。"""
        query_embedding = await embed_search_query(
            query.q,
            user_id=user_id,
            redis=redis,
            daily_max=daily_max,
        )
        analyses, total = await self.search_repo.search_articles(query, query_embedding)

        return PaginatedArticleResponse.create(
            items=[build_brief(a) for a in analyses],
            total=total,
            pagination=query,
        )
