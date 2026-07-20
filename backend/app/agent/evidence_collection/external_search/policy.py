"""External search pipeline が共有する純粋なドメイン規則。"""

from __future__ import annotations

from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_SEARCH_AGENT_HARD_LIMIT,
    EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK,
    EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK,
    EXTERNAL_TASK_QUERY_LIMIT,
    EvidenceSelectionResult,
    ExternalEvidenceSelectionDraft,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
)

__all__ = [
    "EVIDENCE_SELECT_TIMEOUT_SECONDS",
    "PROVIDER_SEARCH_TIMEOUT_SECONDS",
    "QUERY_GENERATE_TIMEOUT_SECONDS",
    "SELECTOR_ERROR_REASON",
    "SELECTOR_TIMEOUT_REASON",
    "build_candidate_pool",
    "build_external_evidence",
    "clean_generated_queries",
    "deduplicate_external_evidence_by_url",
    "finalize_selection_draft",
    "resolve_provider_failure_reason",
    "resolve_external_search_agent_count",
]

QUERY_GENERATE_TIMEOUT_SECONDS = 30
PROVIDER_SEARCH_TIMEOUT_SECONDS = 15
EVIDENCE_SELECT_TIMEOUT_SECONDS = 30
SELECTOR_TIMEOUT_REASON = "selector_timeout"
SELECTOR_ERROR_REASON = "selector_error"


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


def build_candidate_pool(
    query_candidates: list[list[ExternalSearchCandidate]],
) -> list[ExternalSearchCandidate]:
    pool: list[ExternalSearchCandidate] = []
    seen_urls: set[str] = set()
    max_candidates = max(
        (len(candidates) for candidates in query_candidates),
        default=0,
    )
    for offset in range(max_candidates):
        for candidates in query_candidates:
            if offset >= len(candidates):
                continue
            candidate = candidates[offset]
            url = str(candidate.url)
            if url in seen_urls:
                continue
            pool.append(candidate)
            seen_urls.add(url)
            if len(pool) >= EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK:
                return pool
    return pool


def build_external_evidence(
    *,
    task_index: int,
    pool: list[ExternalSearchCandidate],
    selection_result: EvidenceSelectionResult,
) -> tuple[list[ExternalSearchEvidence], int]:
    evidence: list[ExternalSearchEvidence] = []
    selected_indexes: set[int] = set()
    dropped_selection_count = 0

    for selection in selection_result.selections:
        if selection.candidate_index >= len(pool):
            dropped_selection_count += 1
            continue
        if selection.candidate_index in selected_indexes:
            dropped_selection_count += 1
            continue
        if len(evidence) >= EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK:
            dropped_selection_count += 1
            continue

        candidate = pool[selection.candidate_index]
        selected_indexes.add(selection.candidate_index)
        evidence.append(
            ExternalSearchEvidence(
                source_ref=f"external-{task_index}-{selection.candidate_index}",
                task_index=task_index,
                claim=selection.claim,
                why_selected=selection.why_selected,
                url=candidate.url,
                title=candidate.title,
                snippet=candidate.snippet,
                published_at=candidate.published_at,
                source_name=candidate.source_name,
            )
        )

    return evidence, dropped_selection_count


def deduplicate_external_evidence_by_url(
    evidence: list[ExternalSearchEvidence],
) -> tuple[list[ExternalSearchEvidence], int]:
    deduplicated: list[ExternalSearchEvidence] = []
    seen_urls: set[str] = set()
    dropped_count = 0
    for item in evidence:
        url = str(item.url)
        if url in seen_urls:
            dropped_count += 1
            continue
        deduplicated.append(item)
        seen_urls.add(url)
    return deduplicated, dropped_count


def finalize_selection_draft(
    draft: ExternalEvidenceSelectionDraft,
) -> EvidenceSelectionResult:
    return EvidenceSelectionResult.from_raw(
        selections=[selection.model_dump() for selection in draft.selections],
        missing=draft.missing,
    )


def resolve_provider_failure_reason(
    *,
    reason: str | None,
    code: str | None,
) -> str:
    if reason is not None:
        return reason
    if code is not None:
        return code
    return SELECTOR_ERROR_REASON


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
