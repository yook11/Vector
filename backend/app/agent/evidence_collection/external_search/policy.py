"""External search pipeline が共有する純粋なドメイン規則。"""

from __future__ import annotations

from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_SEARCH_AGENT_HARD_LIMIT,
    EXTERNAL_SEARCH_HIT_POOL_LIMIT_PER_TASK,
    EXTERNAL_TASK_QUERY_LIMIT,
    ExternalSearchHit,
)

__all__ = [
    "PROVIDER_SEARCH_TIMEOUT_SECONDS",
    "QUERY_GENERATE_TIMEOUT_SECONDS",
    "build_hit_pool",
    "clean_generated_queries",
    "resolve_external_search_agent_count",
]

QUERY_GENERATE_TIMEOUT_SECONDS = 30
PROVIDER_SEARCH_TIMEOUT_SECONDS = 15


def clean_generated_queries(raw_queries: list[str]) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for raw_query in raw_queries:
        if not isinstance(raw_query, str):
            continue
        query = raw_query.strip()[:EXTERNAL_QUERY_MAX_CHARS]
        if not query or query in seen:
            continue
        queries.append(query)
        seen.add(query)
        if len(queries) >= EXTERNAL_TASK_QUERY_LIMIT:
            break
    return queries


def build_hit_pool(
    hits_by_query: list[list[ExternalSearchHit]],
) -> list[ExternalSearchHit]:
    pool: list[ExternalSearchHit] = []
    seen_urls: set[str] = set()
    max_hits = max(
        (len(hits) for hits in hits_by_query),
        default=0,
    )
    for offset in range(max_hits):
        for hits in hits_by_query:
            if offset >= len(hits):
                continue
            hit = hits[offset]
            url = str(hit.url)
            if url in seen_urls:
                continue
            pool.append(hit)
            seen_urls.add(url)
            if len(pool) >= EXTERNAL_SEARCH_HIT_POOL_LIMIT_PER_TASK:
                return pool
    return pool


def resolve_external_search_agent_count(
    *,
    task_count: int,
    requested_agent_count: int | None = None,
) -> int:
    """設定値を hard limit 3 と task 数で丸めた実効 agent 数にする。"""

    if task_count <= 0:
        return 0

    requested = task_count if requested_agent_count is None else requested_agent_count
    safe_requested = max(1, requested)
    return min(task_count, safe_requested, EXTERNAL_SEARCH_AGENT_HARD_LIMIT)
