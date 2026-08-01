"""1つの ResearchTask に対して内部候補と外部候補を集める Researcher。

DB / Redis / HTTP client の生成は composition が所有し、Researcher は
渡された Tool と Runtime だけを使う(責任境界: Researcher は infrastructure
の構築を知らない)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.agent.concurrency import gather_cancel_on_error
from app.agent.contract import (
    AnswerEventReporter,
    AnswerProgressEvent,
    ExternalSearchCandidatesFetchedEvent,
    ExternalSearchQueriesGeneratedEvent,
    InternalSearchCompletedEvent,
    InternalSearchStartedEvent,
)
from app.agent.evidence_collection.external_search.agent import EXTERNAL_QUERY_AGENT
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
    ExternalQueryGenerationInput,
    ExternalResearchRuntime,
    ExternalSearchCandidate,
    ExternalSearchDateFilter,
    ExternalSearchProviderError,
    ExternalSearchTool,
    ExternalSearchToolInput,
)
from app.agent.evidence_collection.external_search.policy import (
    PROVIDER_SEARCH_TIMEOUT_SECONDS,
    QUERY_GENERATE_TIMEOUT_SECONDS,
    build_candidate_pool,
    clean_generated_queries,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
    InternalSearchError,
    InternalSearchTool,
    InternalSearchToolInput,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.phase_span import agent_phase
from app.agent.planning.contract import (
    ExternalResearchTask,
    ResearchTask,
    TargetTimeWindow,
)
from app.agent.runtime.contract import AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["ExternalCollectionStatus", "Researcher", "ResearchTaskCandidates"]

ExternalCollectionStatus = Literal[
    "succeeded", "query_generation_failed", "provider_failed"
]


@dataclass(frozen=True, slots=True)
class ResearchTaskCandidates:
    """1 task分の内部候補と外部候補の収集結果。選別前のraw candidate。"""

    internal_hits: list[InternalArticleSearchHit]
    internal_failed: bool
    generated_queries: list[str]
    provider_failed_query_count: int
    candidate_pool: list[ExternalSearchCandidate]
    external_status: ExternalCollectionStatus | None


@dataclass(frozen=True, slots=True)
class Researcher:
    """1つのResearchTaskについて内部・外部候補を集める。選別と回答生成は持たない。"""

    internal_search: InternalSearchTool
    events: AnswerEventReporter | None = None

    async def collect(
        self,
        *,
        task_index: int,
        task: ResearchTask,
        external: ExternalResearchRuntime | None,
        date_filter: ExternalSearchDateFilter | None,
        as_of: datetime,
        target_time_window: TargetTimeWindow | None = None,
    ) -> ResearchTaskCandidates:
        internal_result, external_result = await _gather_two_branches(
            self._collect_internal(task_index=task_index, task=task),
            self._collect_external(
                task_index=task_index,
                task=task,
                external=external,
                date_filter=date_filter,
                target_time_window=target_time_window,
                as_of=as_of,
            ),
        )
        hits, internal_failed = _raise_if_exception(internal_result)
        (
            generated_queries,
            provider_failed_query_count,
            candidate_pool,
            external_status,
        ) = _raise_if_exception(external_result)
        return ResearchTaskCandidates(
            internal_hits=hits,
            internal_failed=internal_failed,
            generated_queries=generated_queries,
            provider_failed_query_count=provider_failed_query_count,
            candidate_pool=candidate_pool,
            external_status=external_status,
        )

    async def _collect_internal(
        self,
        *,
        task_index: int,
        task: ResearchTask,
    ) -> tuple[list[InternalArticleSearchHit], bool]:
        queries = InternalSearchQueries(queries=tuple(task.article_search_queries))
        await self._report_event(
            InternalSearchStartedEvent(
                task_index=task_index,
                query_count=len(queries.queries),
            )
        )
        try:
            # 失敗はspanを貫通させてtraceに残し、外側で縮退へ変える。
            with agent_phase(phase="evidence_collection", task_index=task_index):
                hits = await self.internal_search.invoke(
                    InternalSearchToolInput(queries=queries)
                )
        except InternalSearchError:
            return [], True
        await self._report_event(
            InternalSearchCompletedEvent(task_index=task_index, hit_count=len(hits))
        )
        return hits, False

    async def _collect_external(
        self,
        *,
        task_index: int,
        task: ResearchTask,
        external: ExternalResearchRuntime | None,
        date_filter: ExternalSearchDateFilter | None,
        target_time_window: TargetTimeWindow | None,
        as_of: datetime,
    ) -> tuple[
        list[str], int, list[ExternalSearchCandidate], ExternalCollectionStatus | None
    ]:
        if external is None:
            return [], 0, [], None

        query_input = ExternalQueryGenerationInput(
            task=ExternalResearchTask(research_goal=task.research_goal),
            as_of=as_of,
            target_time_window=target_time_window,
        )
        with agent_phase(
            phase="evidence_collection",
            agent_name=EXTERNAL_QUERY_AGENT.name,
            task_index=task_index,
        ):
            try:
                query_draft = await asyncio.wait_for(
                    external.query_runtime.invoke(
                        EXTERNAL_QUERY_AGENT,
                        query_input,
                        attempt_number=1,
                    ),
                    timeout=QUERY_GENERATE_TIMEOUT_SECONDS,
                )
            except (AgentResponseInvalidError, AIProviderError, TimeoutError):
                return [], 0, [], "query_generation_failed"

        queries = clean_generated_queries(query_draft.queries)
        if not queries:
            return [], 0, [], "query_generation_failed"
        await self._report_event(
            ExternalSearchQueriesGeneratedEvent(task_index=task_index, queries=queries)
        )

        query_candidates: list[list[ExternalSearchCandidate]] = []
        provider_failed_query_count = 0
        provider_results = await gather_cancel_on_error(
            *[
                self._search_external_query(
                    query,
                    search_tool=external.search_tool,
                    date_filter=date_filter,
                )
                for query in queries
            ]
        )
        for candidates, failed in provider_results:
            if failed:
                provider_failed_query_count += 1
                query_candidates.append([])
                continue
            query_candidates.append(candidates)

        if provider_failed_query_count == len(queries):
            return queries, provider_failed_query_count, [], "provider_failed"

        pool = build_candidate_pool(query_candidates)
        await self._report_event(
            ExternalSearchCandidatesFetchedEvent(
                task_index=task_index,
                candidate_count=len(pool),
            )
        )
        return queries, provider_failed_query_count, pool, "succeeded"

    async def _search_external_query(
        self,
        query: str,
        *,
        search_tool: ExternalSearchTool,
        date_filter: ExternalSearchDateFilter | None,
    ) -> tuple[list[ExternalSearchCandidate], bool]:
        try:
            candidates = await asyncio.wait_for(
                search_tool.invoke(
                    ExternalSearchToolInput(
                        query=query,
                        limit=EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
                        date_filter=date_filter,
                    )
                ),
                timeout=PROVIDER_SEARCH_TIMEOUT_SECONDS,
            )
        except (ExternalSearchProviderError, TimeoutError):
            return [], True
        return candidates[:EXTERNAL_SEARCH_CANDIDATES_PER_QUERY], False

    async def _report_event(self, event: AnswerProgressEvent) -> None:
        if self.events is None:
            return
        await self.events.event_occurred(event)


async def _gather_two_branches[FirstT, SecondT](
    first: Awaitable[FirstT],
    second: Awaitable[SecondT],
) -> tuple[FirstT | BaseException, SecondT | BaseException]:
    """内部枝と外部枝を両方settleさせてから結果を返す(片方が例外でも他方を待つ)。"""
    tasks = [asyncio.create_task(first), asyncio.create_task(second)]
    gathered = asyncio.gather(*tasks, return_exceptions=True)
    try:
        results = await asyncio.shield(gathered)
    except asyncio.CancelledError as exc:
        for task in tasks:
            task.cancel()
        await gathered
        raise exc
    return results[0], results[1]


def _raise_if_exception[ResultT](result: ResultT | BaseException) -> ResultT:
    if isinstance(result, BaseException):
        raise result
    return result
