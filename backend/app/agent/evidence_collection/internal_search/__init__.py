"""Internal search package."""

from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
    PgVectorArticleSearchRepository,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalSearchError,
    InternalSearchFailurePhase,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    MAX_ARTICLE_SEARCH_QUERIES,
    InternalQueryEmbedder,
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.agent.evidence_collection.internal_search.query_embedding_cache import (
    QueryEmbeddingCacheRepository,
    TransactionalQueryEmbeddingCache,
)
from app.agent.evidence_collection.internal_search.service import InternalSearchService

__all__ = [
    "InternalArticleContent",
    "InternalArticleSearchHit",
    "MAX_ARTICLE_SEARCH_QUERIES",
    "InternalQueryEmbedder",
    "InternalQueryEmbedding",
    "InternalSearchError",
    "InternalSearchFailurePhase",
    "InternalSearchQueries",
    "InternalSearchService",
    "PgVectorArticleSearchRepository",
    "QueryEmbeddingCacheRepository",
    "TransactionalQueryEmbeddingCache",
]
