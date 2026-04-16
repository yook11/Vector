"""セマンティック検索サービス — embedding ベースの分析的探索。"""

from __future__ import annotations

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedder.factory import get_embedder
from app.analysis.errors import AnalysisDomainError
from app.repositories.watchlist import WatchlistRepository
from app.schemas.articles import PaginatedArticleResponse, SemanticSearchParams
from app.search.errors import SearchError
from app.search.repository import SemanticSearchRepository
from app.services.articles import build_brief


async def embed_search_query(
    text: str, embedder: BaseEmbedder | None = None
) -> list[float]:
    """RETRIEVAL_QUERY タスクタイプで検索クエリを embedding 化する。

    まず Redis embedding キャッシュを確認し、miss 時のみ embedder を呼んで
    結果をキャッシュに書き戻す。キャッシュ障害時は直接 API 呼び出しへ
    グレースフルに降格する。

    Args:
        text: Search query text (expected to be pre-normalized by the caller).
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        A list of floats representing the query embedding.

    Raises:
        SearchError: If the API call fails.
    """
    from app.search.embedding_cache import get_query_embedding, set_query_embedding

    cached = await get_query_embedding(text)
    if cached is not None:
        return cached

    if embedder is None:
        embedder = get_embedder()

    try:
        vector = await embedder.embed_query(text)
    except AnalysisDomainError as e:
        raise SearchError(str(e)) from e

    await set_query_embedding(text, vector)
    return vector


class SemanticSearchService:
    def __init__(
        self,
        search_repo: SemanticSearchRepository,
        watchlist_repo: WatchlistRepository,
    ) -> None:
        self.search_repo = search_repo
        self.watchlist_repo = watchlist_repo

    async def search(
        self,
        query: SemanticSearchParams,
        user_id: int | None,
    ) -> PaginatedArticleResponse:
        """ユーザーのクエリテキストとのセマンティック類似度で記事を検索する。"""
        query_embedding = await embed_search_query(query.q)
        analyses, total = await self.search_repo.search_articles(query, query_embedding)

        watched_ids: set[int] = set()
        if user_id and analyses:
            article_ids = {a.id for a in analyses}
            watched_ids = await self.watchlist_repo.watched_among(user_id, article_ids)

        return PaginatedArticleResponse.create(
            items=[build_brief(a, watched_ids) for a in analyses],
            total=total,
            pagination=query,
        )
