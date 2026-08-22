"""1つの ResearchTask に対して内部・外部の検索ヒットを集める ResearchTaskCollector。

DB / Redis / HTTP client の生成は composition が所有し、ResearchTaskCollector は
渡された検索能力と Runtime だけを使う(責任境界: ResearchTaskCollector は infrastructure
の構築を知らない)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

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
    ExternalSearch,
    ExternalSearchDateFilter,
    ExternalSearchHit,
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
from app.agent.planning.contract import ResearchTask, TargetTimeWindow

__all__ = ["ExternalCollectionStatus", "ResearchTaskCollector", "ResearchTaskHits"]

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
class ResearchTaskCollector:
    """1つのResearchTaskについて内部・外部の検索ヒットを集める。精査と回答生成は持たない。"""

    internal_search: InternalSearch
    events: AnswerEventReporter | None = None

    async def collect(
        self,
        *,
        task_index: int,
        task: ResearchTask,
        external_search: ExternalSearch | None,
        date_filter: ExternalSearchDateFilter | None,
        as_of: datetime,
        target_time_window: TargetTimeWindow | None = None,
    ) -> ResearchTaskHits:
        internal_result, external_result = await _gather_two_branches(
            self._collect_internal(task_index=task_index, task=task),
            self._collect_external(
                task_index=task_index,
                task=task,
                external_search=external_search,
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
        external_search: ExternalSearch | None,
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
        if external_search is None:
            return [], 0, [], None, ()

        with agent_phase(
            phase="evidence_collection",
            agent_name=EXTERNAL_QUERY_AGENT.name,
            task_index=task_index,
        ):
            queries = await external_search.generate_queries(
                research_goal=task.research_goal,
                as_of=as_of,
                target_time_window=target_time_window,
            )
        if not queries:
            return [], 0, [], "query_generation_failed", ()
        await self._report_event(
            ExternalSearchQueriesGeneratedEvent(task_index=task_index, queries=queries)
        )

        execution = await external_search.search_queries(
            queries,
            date_filter=date_filter,
        )
        failed_query_count = execution.provider_failed_query_count
        if failed_query_count == len(queries):
            return queries, failed_query_count, [], "provider_failed", ()

        await self._report_event(
            ExternalSearchHitsFetchedEvent(
                task_index=task_index,
                hit_count=len(execution.hits),
            )
        )
        return (
            queries,
            failed_query_count,
            execution.hits,
            "succeeded",
            execution.executed_queries,
        )

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
