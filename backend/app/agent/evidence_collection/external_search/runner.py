"""Goal 起点の research runner 実装。

query 生成 -> provider 検索 -> 候補 pool -> 選別検証を task 並列で実行する。
worker が捕捉するのは分類済み境界 error と TimeoutError のみで、
未分類の例外は握らず伝播させる。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum

import logfire
from opentelemetry.trace import StatusCode
from pydantic import ValidationError

from app.agent.agent import Agent
from app.agent.contract import (
    AnswerEventReporter,
    AnswerProgressEvent,
    ExternalSearchCandidatesFetchedEvent,
    ExternalSearchEvidenceSelectedEvent,
    ExternalSearchQueriesGeneratedEvent,
)
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_QUERY_MAX_CHARS,
    EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK,
    EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
    EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK,
    EXTERNAL_TASK_QUERY_LIMIT,
    EvidenceSelectionResult,
    ExternalEvidenceCandidateInput,
    ExternalEvidenceSelectionDraft,
    ExternalEvidenceSelectionInput,
    ExternalQueryDraft,
    ExternalQueryGenerationInput,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
    ExternalSearchProviderError,
    ExternalSearchRequest,
    ExternalSearchRunResult,
    ExternalSearchTool,
    ExternalSearchToolInput,
    ResearchTaskReport,
    ResearchTaskStatus,
)
from app.agent.planning.contract import ExternalResearchTask
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
    AgentRuntime,
)
from app.analysis.ai_provider_errors import AIProviderError

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
_PHASE_SPAN_NAME = "agent_phase"
_QUERY_PHASE = "external_query"
_SELECTOR_PHASE = "external_selector"


class ExternalSearchResearchRunner:
    """goal 起点の query 生成、検索、evidence 選別を task 並列で実行する。"""

    def __init__(
        self,
        *,
        query_agent: Agent[ExternalQueryGenerationInput, ExternalQueryDraft],
        query_runtime: AgentRuntime,
        search_tool: ExternalSearchTool,
        selector_agent: Agent[
            ExternalEvidenceSelectionInput,
            ExternalEvidenceSelectionDraft,
        ],
        selector_runtime: AgentRuntime,
        events: AnswerEventReporter | None = None,
    ) -> None:
        self._query_agent = query_agent
        self._query_runtime = query_runtime
        self._search_tool = search_tool
        self._selector_agent = selector_agent
        self._selector_runtime = selector_runtime
        self._events = events

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
        query_input = ExternalQueryGenerationInput(
            task=task,
            as_of=request.as_of,
            target_time_window=request.target_time_window,
        )
        with _external_agent_phase(
            phase=_QUERY_PHASE,
            agent_name=self._query_agent.name,
            task_index=task_index,
        ):
            try:
                query_draft = await asyncio.wait_for(
                    self._query_runtime.invoke(
                        self._query_agent,
                        query_input,
                        attempt_number=1,
                    ),
                    timeout=QUERY_GENERATE_TIMEOUT_SECONDS,
                )
            except (AgentResponseInvalidError, AIProviderError, TimeoutError):
                return [], self._task_report(
                    task_index=task_index,
                    task=task,
                    status="query_generation_failed",
                )

        queries = _clean_generated_queries(query_draft.queries)
        if not queries:
            return [], self._task_report(
                task_index=task_index,
                task=task,
                status="query_generation_failed",
            )
        await self._report_event(
            ExternalSearchQueriesGeneratedEvent(
                task_index=task_index,
                queries=queries,
            )
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
        await self._report_event(
            ExternalSearchCandidatesFetchedEvent(
                task_index=task_index,
                candidate_count=len(pool),
            )
        )
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
            task_index=task_index,
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
        await self._report_event(
            ExternalSearchEvidenceSelectedEvent(
                task_index=task_index,
                evidence_count=len(evidence),
            )
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
                self._search_tool.invoke(
                    ExternalSearchToolInput(
                        query=query,
                        limit=EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
                    )
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
        task_index: int,
    ) -> tuple[EvidenceSelectionResult | None, str | None]:
        selector_input = ExternalEvidenceSelectionInput(
            task=task,
            candidates=tuple(
                ExternalEvidenceCandidateInput(
                    index=index,
                    title=candidate.title,
                    source_name=candidate.source_name,
                    published_at=candidate.published_at,
                    snippet=candidate.snippet,
                )
                for index, candidate in enumerate(candidates)
            ),
            as_of=as_of,
        )
        selector_failure_reason: str | None = None
        with _external_agent_phase(
            phase=_SELECTOR_PHASE,
            agent_name=self._selector_agent.name,
            task_index=task_index,
        ):
            for attempt_number in range(1, 3):
                try:
                    draft = await asyncio.wait_for(
                        self._selector_runtime.invoke(
                            self._selector_agent,
                            selector_input,
                            attempt_number=attempt_number,
                        ),
                        timeout=EVIDENCE_SELECT_TIMEOUT_SECONDS,
                    )
                except AgentResponseInvalidError as exc:
                    selector_failure_reason = exc.defect.value
                    continue
                except AIProviderError as exc:
                    selector_failure_reason = _provider_failure_reason(exc)
                    continue
                except TimeoutError:
                    selector_failure_reason = SELECTOR_TIMEOUT_REASON
                    continue

                try:
                    selection_result = _finalize_selection_draft(draft)
                except ValidationError:
                    selector_failure_reason = (
                        AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value
                    )
                    continue
                return selection_result, None
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

    async def _report_event(self, event: AnswerProgressEvent) -> None:
        if self._events is None:
            return
        await self._events.event_occurred(event)


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


def _provider_failure_reason(exc: AIProviderError) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, StrEnum):
        return reason.value
    code = getattr(exc, "CODE", None)
    if isinstance(code, str):
        return code
    return SELECTOR_ERROR_REASON


def _finalize_selection_draft(
    draft: ExternalEvidenceSelectionDraft,
) -> EvidenceSelectionResult:
    return EvidenceSelectionResult.from_raw(
        selections=[selection.model_dump() for selection in draft.selections],
        missing=draft.missing,
    )


@contextmanager
def _external_agent_phase(
    *,
    phase: str,
    agent_name: str,
    task_index: int,
) -> Iterator[None]:
    """External task単位のAgent policy spanを作る。"""
    if task_index < 0:
        raise ValueError("task_index must be non-negative")
    with logfire.span(
        _PHASE_SPAN_NAME,
        phase=phase,
        agent_name=agent_name,
        task_index=task_index,
    ) as span:
        try:
            yield
        except BaseException:
            span.set_status(StatusCode.ERROR, "unclassified agent phase error")
            raise


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
