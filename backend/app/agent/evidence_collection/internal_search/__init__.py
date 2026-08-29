"""Internal search package。公開名は package root から import する。"""

from app.agent.evidence_collection.internal_search.article_repository import (
    PgVectorArticleSearchRepository,
)
from app.agent.evidence_collection.internal_search.contract import (
    INTERNAL_SEARCH_HIT_POOL_LIMIT,
    INTERNAL_SEARCH_HITS_PER_QUERY,
    InternalArticleContent,
    InternalArticleSearchHit,
    InternalSearch,
    InternalSearchError,
    InternalSearchFailureCode,
    InternalSearchOutcome,
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
from app.agent.evidence_collection.internal_search.service import (
    InternalSearchService,
)

__all__ = [
    "INTERNAL_SEARCH_HIT_POOL_LIMIT",
    "INTERNAL_SEARCH_HITS_PER_QUERY",
    "InternalArticleContent",
    "InternalArticleSearchHit",
    "MAX_ARTICLE_SEARCH_QUERIES",
    "InternalQueryEmbedder",
    "InternalQueryEmbedding",
    "InternalSearch",
    "InternalSearchError",
    "InternalSearchFailureCode",
    "InternalSearchOutcome",
    "InternalSearchQueries",
    "InternalSearchService",
    "PgVectorArticleSearchRepository",
    "QueryEmbeddingCacheRepository",
    "TransactionalQueryEmbeddingCache",
]
