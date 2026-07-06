"""Goal 起点の research runner 実装。

query 生成 -> provider 検索 -> 候補 pool -> 選別検証を task 並列で実行する。
worker が捕捉するのは分類済み境界 error と TimeoutError のみで、
未分類の例外は握らず伝播させる。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.agent.contract import ExternalResearchTask
from app.agent.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK,
    EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
    EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK,
    EXTERNAL_TASK_QUERY_LIMIT,
    EvidenceSelectionResult,
    EvidenceSelector,
    ExternalEvidenceSelectorError,
    ExternalQueryGenerationError,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
    ExternalSearchProviderError,
    ExternalSearchRequest,
    ExternalSearchRunResult,
    QueryGenerator,
    ResearchTaskReport,
    ResearchTaskStatus,
    SearchProvider,
)

__all__ = [
    "EVIDENCE_SELECT_TIMEOUT_SECONDS",
    "PROVIDER_SEARCH_TIMEOUT_SECONDS",
    "QUERY_GENERATE_TIMEOUT_SECONDS",
    "ExternalSearchResearchRunner",
]

QUERY_GENERATE_TIMEOUT_SECONDS = 30
PROVIDER_SEARCH_TIMEOUT_SECONDS = 15
EVIDENCE_SELECT_TIMEOUT_SECONDS = 30
SELECTOR_TIMEOUT_REASON = "selector_timeout"
SELECTOR_ERROR_REASON = "selector_error"


class ExternalSearchResearchRunner:
    """goal 起点の query 生成、検索、evidence 選別を task 並列で実行する。"""

    def __init__(
        self,
        *,
        query_generator: QueryGenerator,
        search_provider: SearchProvider,
        evidence_selector: EvidenceSelector,
    ) -> None:
        self._query_generator = query_generator
        self._search_provider = search_provider
        self._evidence_selector = evidence_selector

    async def search(self, request: ExternalSearchRequest) -> ExternalSearchRunResult:
        if not request.tasks:
            return ExternalSearchRunResult()

        semaphore = asyncio.Semaphore(max(1, request.effective_agent_count))

        async def run_task(
            task_index: int,
            task: ExternalResearchTask,
        ) -> tuple[list[ExternalSearchEvidence], ResearchTaskReport]:
            async with semaphore:
                return await self._search_task(
                    request=request,
                    task_index=task_index,
                    task=task,
                )

        results = await asyncio.gather(
            *[
                run_task(task_index, task)
                for task_index, task in enumerate(request.tasks)
            ]
        )
        evidence: list[ExternalSearchEvidence] = []
        reports: list[ResearchTaskReport] = []
        for task_evidence, report in results:
            evidence.extend(task_evidence)
            reports.append(report)
        return ExternalSearchRunResult(evidence=evidence, task_reports=reports)

    async def _search_task(
        self,
        *,
        request: ExternalSearchRequest,
        task_index: int,
        task: ExternalResearchTask,
    ) -> tuple[list[ExternalSearchEvidence], ResearchTaskReport]:
        try:
            generated_queries = await asyncio.wait_for(
                self._query_generator.generate(
                    task=task,
                    as_of=request.as_of,
                    target_time_window=request.target_time_window,
                ),
                timeout=QUERY_GENERATE_TIMEOUT_SECONDS,
            )
        except (ExternalQueryGenerationError, TimeoutError):
            return [], self._task_report(
                task_index=task_index,
                task=task,
                status="query_generation_failed",
            )

        queries = _clean_generated_queries(generated_queries)
        if not queries:
            return [], self._task_report(
                task_index=task_index,
                task=task,
                status="query_generation_failed",
            )

        query_candidates: list[list[ExternalSearchCandidate]] = []
        provider_failed_query_count = 0
        provider_results = await asyncio.gather(
            *[self._search_query(query) for query in queries],
        )
        for candidates, failed in provider_results:
            if failed:
                provider_failed_query_count += 1
                query_candidates.append([])
                continue
            query_candidates.append(candidates)

        if provider_failed_query_count == len(queries):
            return [], self._task_report(
                task_index=task_index,
                task=task,
                status="provider_failed",
                generated_queries=queries,
                provider_failed_query_count=provider_failed_query_count,
            )

        pool = _build_candidate_pool(query_candidates)
        if not pool:
            return [], self._task_report(
                task_index=task_index,
                task=task,
                status="succeeded",
                generated_queries=queries,
                provider_failed_query_count=provider_failed_query_count,
                candidate_count=0,
            )

        selection_result, selector_failure_reason = await self._select_evidence(
            task=task,
            candidates=pool,
            as_of=request.as_of,
        )
        if selection_result is None:
            return [], self._task_report(
                task_index=task_index,
                task=task,
                status="selector_failed",
                generated_queries=queries,
                provider_failed_query_count=provider_failed_query_count,
                candidate_count=len(pool),
                selector_failure_reason=selector_failure_reason,
            )

        evidence, dropped_selection_count = _build_evidence(
            task_index=task_index,
            pool=pool,
            selection_result=selection_result,
        )
        return evidence, self._task_report(
            task_index=task_index,
            task=task,
            status="succeeded",
            generated_queries=queries,
            provider_failed_query_count=provider_failed_query_count,
            candidate_count=len(pool),
            evidence_count=len(evidence),
            dropped_selection_count=dropped_selection_count,
            missing=selection_result.missing,
        )

    async def _search_query(
        self,
        query: str,
    ) -> tuple[list[ExternalSearchCandidate], bool]:
        try:
            candidates = await asyncio.wait_for(
                self._search_provider.search(
                    query,
                    limit=EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
                ),
                timeout=PROVIDER_SEARCH_TIMEOUT_SECONDS,
            )
        except (ExternalSearchProviderError, TimeoutError):
            return [], True
        return candidates[:EXTERNAL_SEARCH_CANDIDATES_PER_QUERY], False

    async def _select_evidence(
        self,
        *,
        task: ExternalResearchTask,
        candidates: list[ExternalSearchCandidate],
        as_of: datetime,
    ) -> tuple[EvidenceSelectionResult | None, str | None]:
        selector_failure_reason: str | None = None
        for _ in range(2):
            try:
                return (
                    await asyncio.wait_for(
                        self._evidence_selector.select(
                            task=task,
                            candidates=candidates,
                            as_of=as_of,
                        ),
                        timeout=EVIDENCE_SELECT_TIMEOUT_SECONDS,
                    ),
                    None,
                )
            except ExternalEvidenceSelectorError as exc:
                selector_failure_reason = _selector_failure_reason(exc)
            except TimeoutError:
                selector_failure_reason = SELECTOR_TIMEOUT_REASON
        return None, selector_failure_reason

    def _task_report(
        self,
        *,
        task_index: int,
        task: ExternalResearchTask,
        status: ResearchTaskStatus,
        generated_queries: list[str] | None = None,
        provider_failed_query_count: int = 0,
        candidate_count: int = 0,
        evidence_count: int = 0,
        dropped_selection_count: int = 0,
        selector_failure_reason: str | None = None,
        missing: list[str] | None = None,
    ) -> ResearchTaskReport:
        return ResearchTaskReport.from_raw(
            task_index=task_index,
            collection_goal=task.collection_goal,
            generated_queries=generated_queries,
            status=status,
            provider_failed_query_count=provider_failed_query_count,
            candidate_count=candidate_count,
            evidence_count=evidence_count,
            dropped_selection_count=dropped_selection_count,
            selector_failure_reason=selector_failure_reason,
            missing=missing,
        )


def _clean_generated_queries(raw_queries: list[str]) -> list[str]:
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


def _selector_failure_reason(exc: ExternalEvidenceSelectorError) -> str:
    if exc.reason:
        return str(exc.reason)
    return SELECTOR_ERROR_REASON


def _build_candidate_pool(
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


def _build_evidence(
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
