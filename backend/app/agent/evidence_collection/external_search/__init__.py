"""External search package。公開名は package root から import する。"""

from app.agent.evidence_collection.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EVIDENCE_WHY_SELECTED_MAX_CHARS,
    EXTERNAL_CONTENT_MAX_CHARS,
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_SEARCH_HIT_POOL_LIMIT_PER_TASK,
    EXTERNAL_SEARCH_HITS_PER_QUERY,
    EXTERNAL_TASK_QUERY_LIMIT,
    MISSING_ITEM_MAX_CHARS,
    ExternalQueryDraft,
    ExternalQueryGenerationInput,
    ExternalSearch,
    ExternalSearchDateFilter,
    ExternalSearchExecution,
    ExternalSearchFailureCode,
    ExternalSearchFailureReason,
    ExternalSearchGateway,
    ExternalSearchHit,
    ExternalSearchProviderError,
    ExternalSearchRequest,
    ExternalSearchScopeFactory,
    TimeFilterFailureReason,
)
from app.agent.evidence_collection.external_search.policy import (
    PROVIDER_SEARCH_TIMEOUT_SECONDS,
    QUERY_GENERATE_TIMEOUT_SECONDS,
)
from app.agent.evidence_collection.external_search.service import (
    ExternalSearchService,
)
from app.agent.evidence_collection.external_search.tavily import (
    TavilyExternalSearchGateway,
)
from app.agent.evidence_collection.external_search.tavily_spec import (
    TAVILY_NEWS_SEARCH_SPEC,
    TavilySearchCallSpec,
    build_search_body,
)
from app.agent.evidence_collection.external_search.time_filter import (
    ExternalSearchDateFilterResolutionError,
    resolve_external_search_date_filter,
)

__all__ = [
    "EVIDENCE_CLAIM_MAX_CHARS",
    "EVIDENCE_WHY_SELECTED_MAX_CHARS",
    "EXTERNAL_CONTENT_MAX_CHARS",
    "EXTERNAL_QUERY_MAX_CHARS",
    "EXTERNAL_SEARCH_HITS_PER_QUERY",
    "EXTERNAL_SEARCH_HIT_POOL_LIMIT_PER_TASK",
    "EXTERNAL_TASK_QUERY_LIMIT",
    "ExternalQueryDraft",
    "ExternalQueryGenerationInput",
    "ExternalSearchHit",
    "ExternalSearch",
    "ExternalSearchDateFilter",
    "ExternalSearchExecution",
    "ExternalSearchFailureCode",
    "ExternalSearchFailureReason",
    "ExternalSearchGateway",
    "ExternalSearchProviderError",
    "ExternalSearchRequest",
    "ExternalSearchScopeFactory",
    "ExternalSearchService",
    "MISSING_ITEM_MAX_CHARS",
    "PROVIDER_SEARCH_TIMEOUT_SECONDS",
    "QUERY_GENERATE_TIMEOUT_SECONDS",
    "TAVILY_NEWS_SEARCH_SPEC",
    "TavilyExternalSearchGateway",
    "TavilySearchCallSpec",
    "TimeFilterFailureReason",
    "ExternalSearchDateFilterResolutionError",
    "build_search_body",
    "resolve_external_search_date_filter",
]
