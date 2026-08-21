"""1つの ResearchTask に対して内部・外部の検索ヒットを集める Researcher。

DB / Redis / HTTP client の生成は composition が所有し、Researcher は
渡された検索能力と Runtime だけを使う(責任境界: Researcher は infrastructure
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
    ExternalSearchHitsFetchedEvent,
    ExternalSearchQueriesGeneratedEvent,
    InternalSearchCompletedEvent,
    InternalSearchStartedEvent,
)
from app.agent.evidence_collection.external_search.agent import EXTERNAL_QUERY_AGENT
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_HITS_PER_QUERY,
    ExternalQueryGenerationInput,
    ExternalResearchRuntime,
    ExternalSearchDateFilter,
    ExternalSearchGateway,
    ExternalSearchHit,
    ExternalSearchProviderError,
    ExternalSearchRequest,
)
from app.agent.evidence_collection.external_search.policy import (
    PROVIDER_SEARCH_TIMEOUT_SECONDS,
    QUERY_GENERATE_TIMEOUT_SECONDS,
    build_hit_pool,
    clean_generated_queries,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
    InternalSearch,
    InternalSearchError,
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

__all__ = ["ExternalCollectionStatus", "Researcher", "ResearchTaskHits"]

ExternalCollectionStatus = Literal[
    "succeeded", "query_generation_failed", "provider_failed"
]


@dataclass(frozen=True, slots=True)
class ResearchTaskHits:
    """1 task分の内部・外部の検索ヒット。精査前のraw hit。"""

    internal_hits: list[InternalArticleSearchHit]
    internal_failed: bool
    generated_queries: list[str]
    provider_failed_query_count: int
    external_hits: list[ExternalSearchHit]
    external_status: ExternalCollectionStatus | None
    executed_queries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Researcher:
    """1つのResearchTaskについて内部・外部の検索ヒットを集める。精査と回答生成は持たない。"""

    internal_search: InternalSearch
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
    ) -> ResearchTaskHits:
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
            external_hits,
            external_status,
            executed_queries,
        ) = _raise_if_exception(external_result)
        return ResearchTaskHits(
            internal_hits=hits,
            internal_failed=internal_failed,
            generated_queries=generated_queries,
            provider_failed_query_count=provider_failed_query_count,
            external_hits=external_hits,
            external_status=external_status,
            executed_queries=executed_queries,
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
                hits = await self.internal_search.search(queries)
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
        list[str],
        int,
        list[ExternalSearchHit],
        ExternalCollectionStatus | None,
        tuple[str, ...],
    ]:
        if external is None:
            return [], 0, [], None, ()

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
                    external.query_runtime.call(
                        EXTERNAL_QUERY_AGENT,
                        query_input,
                        attempt_number=1,
                    ),
                    timeout=QUERY_GENERATE_TIMEOUT_SECONDS,
                )
            except (AgentResponseInvalidError, AIProviderError, TimeoutError):
                return [], 0, [], "query_generation_failed", ()

        queries = clean_generated_queries(query_draft.queries)
        if not queries:
            return [], 0, [], "query_generation_failed", ()
        await self._report_event(
            ExternalSearchQueriesGeneratedEvent(task_index=task_index, queries=queries)
        )

        hits_by_query: list[list[ExternalSearchHit]] = []
        executed_queries: list[str] = []
        provider_failed_query_count = 0
        # gather_cancel_on_errorはasyncio.gatherに委譲しており、結果順は
        # 完了順でなく渡したawaitablesの順(=queriesの順)と一致する。
        provider_results = await gather_cancel_on_error(
            *[
                self._search_external_query(
                    query,
                    search_gateway=external.search_gateway,
                    date_filter=date_filter,
                )
                for query in queries
            ]
        )
        for query, (hits, failed) in zip(queries, provider_results, strict=True):
            if failed:
                provider_failed_query_count += 1
                hits_by_query.append([])
                continue
            hits_by_query.append(hits)
            executed_queries.append(query)

        if provider_failed_query_count == len(queries):
            return queries, provider_failed_query_count, [], "provider_failed", ()

        pool = build_hit_pool(hits_by_query)
        await self._report_event(
            ExternalSearchHitsFetchedEvent(
                task_index=task_index,
                hit_count=len(pool),
            )
        )
        return (
            queries,
            provider_failed_query_count,
            pool,
            "succeeded",
            tuple(executed_queries),
        )

    async def _search_external_query(
        self,
        query: str,
        *,
        search_gateway: ExternalSearchGateway,
        date_filter: ExternalSearchDateFilter | None,
    ) -> tuple[list[ExternalSearchHit], bool]:
        try:
            hits = await asyncio.wait_for(
                search_gateway.search(
                    ExternalSearchRequest(
                        query=query,
                        limit=EXTERNAL_SEARCH_HITS_PER_QUERY,
                        date_filter=date_filter,
                    )
                ),
                timeout=PROVIDER_SEARCH_TIMEOUT_SECONDS,
            )
        except (ExternalSearchProviderError, TimeoutError):
            return [], True
        return hits[:EXTERNAL_SEARCH_HITS_PER_QUERY], False

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
