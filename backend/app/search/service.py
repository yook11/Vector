"""セマンティック検索サービス — embedding ベースの分析的探索。"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as aioredis
import structlog

from app.analysis.errors.provider import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.exceptions import InvalidQueryError
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.search.embedding.base import QueryEmbedder
from app.search.errors import SearchError
from app.search.quota import consume_search_quota
from app.search.repository import SemanticSearchRepository
from app.services.articles import build_brief

logger = structlog.get_logger(__name__)


# provider/infra 起因 (user の query 変更で直らない) は 503 に振る。
# ``AIProviderRequestInvalidError`` も含めるのは、Gemini 応答の shape 違反 (embeddings
# 空 / values None) が provider 側の障害であり user query の問題ではないため。
# ``AIProviderInputRejectedError`` (safety filter blocked) は user query 起因のため
# 422 経路に振る (下の except 節)。
_SEARCH_INFRA_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderQuotaExhaustedError,
    AIProviderRequestInvalidError,
)


async def embed_search_query(
    text: str,
    *,
    user_id: UUID,
    redis: aioredis.Redis,
    daily_max: int,
    embedder: QueryEmbedder,
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
        embedder: 呼び出し側で composition root から injection 済の Embedder。
            本番経路では ``GeminiQueryEmbedder``。CI / Schemathesis 等は
            ``dependency_overrides`` でテスト用 stub に差し替える。

    Returns:
        A list of floats representing the query embedding.

    Raises:
        SearchQuotaExceededError: cache miss 時にユーザーが当日のクォータを使い切った。
        SearchError: provider/infra 起因 (configuration / network / 5xx / rate
            limit / quota / request invalid shape) → 503。
        InvalidQueryError: user query 起因 (safety filter blocked) または翻訳網
            漏れの未知 SDK 例外 → 422。
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

    try:
        vector = await embedder.embed_query(text)
    except _SEARCH_INFRA_PROVIDER_ERRORS as e:
        # provider/infra 起因 (RateLimit / 5xx / network / configuration / quota /
        # provider response shape 違反) → 503 維持。retry or 運用対応が筋。
        raise SearchError(str(e)) from e
    except AIProviderInputRejectedError as e:
        # safety filter blocked 等 — user の query 内容が原因 → 422。
        # 生 query は焼かず長さのみ log (PII / 機微情報の意図せぬ漏出を避ける)。
        logger.info("embed_query_input_rejected", q_len=len(text))
        raise InvalidQueryError(
            "Could not generate embedding for the search query."
        ) from e
    except Exception as e:
        # 翻訳の網を抜けた未知 SDK 例外または translator バグ → 422 維持。
        # Schemathesis の `not_a_server_error` は 5xx 全部 fail にするため
        # 422 として返す。trace は logger.exception で残す (生 query は焼かない)。
        logger.exception("unexpected_embed_query_failure", q_len=len(text))
        raise InvalidQueryError(
            "Could not generate embedding for the search query."
        ) from e

    try:
        await set_query_embedding(text, vector)
    except Exception:
        # cache write 失敗は探索結果には影響しないので warn して握りつぶす。
        # cache miss の次回も同経路を辿るだけなので冪等性に問題なし。
        logger.warning("set_query_embedding_failed", q_len=len(text))
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
        embedder: QueryEmbedder,
    ) -> PaginatedArticleResponse:
        """ユーザーのクエリテキストとのセマンティック類似度で記事を検索する。"""
        query_embedding = await embed_search_query(
            query.q,
            user_id=user_id,
            redis=redis,
            daily_max=daily_max,
            embedder=embedder,
        )
        analyses, total = await self.search_repo.search_articles(query, query_embedding)

        return PaginatedArticleResponse.create(
            items=[build_brief(a) for a in analyses],
            total=total,
            pagination=query,
        )
