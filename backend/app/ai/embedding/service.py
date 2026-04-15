"""Embedding service — orchestration and caching."""

from __future__ import annotations

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedder.factory import get_embedder
from app.models.news_article import NewsArticle


async def embed_search_query(
    text: str, embedder: BaseEmbedder | None = None
) -> list[float]:
    """Embed a search query using RETRIEVAL_QUERY task type.

    Checks the Redis embedding cache first; on miss, calls the embedder and
    writes the result back to the cache. Cache failures degrade gracefully to
    a direct API call.

    Args:
        text: Search query text (expected to be pre-normalized by the caller).
        embedder: Embedder instance; defaults to get_embedder().

    Returns:
        A list of floats representing the query embedding.

    Raises:
        EmbeddingError: If the API call fails.
    """
    from app.infra.redis.embedding_cache import get_query_embedding, set_query_embedding

    cached = await get_query_embedding(text)
    if cached is not None:
        return cached

    if embedder is None:
        embedder = get_embedder()
    vector = await embedder.embed_query(text)
    await set_query_embedding(text, vector)
    return vector


def _build_embed_text(article: NewsArticle) -> str:
    """Build the canonical text to embed for a news article."""
    body = article.original_content or article.original_description or ""
    return f"{article.original_title}\n{body}"
