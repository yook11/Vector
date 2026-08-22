"""Run単位のニュース収集。各research goalへ内部検索と外部検索を並列に行う。"""

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
from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    ResearchTaskReport,
    TaskInternalCollectionStatus,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearch,
    ExternalSearchHit,
    ExternalSearchScopeFactory,
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
from app.agent.planning.contract import ResearchTask, SearchPlan, TargetTimeWindow

__all__ = ["EvidenceCollectionService"]

_ExternalBranchStatus = Literal[
    "succeeded", "query_generation_failed", "provider_failed"
]


@dataclass(frozen=True, slots=True)
class EvidenceCollectionService:
    """Run単位でニュースを収集する。精査と回答生成は持たない。"""

    internal_search: InternalSearch
    external_search_scope_factory: ExternalSearchScopeFactory
    events: AnswerEventReporter | None = None

    async def collect(
        self,
        *,
        plan: SearchPlan,
        as_of: datetime,
    ) -> CollectedNews:
        tasks = plan.research_tasks

        async with self.external_search_scope_factory() as external_search:

            async def run_task(task_index: int, task: ResearchTask) -> CollectedTask:
                return await self._collect_for_goal(
                    task_index=task_index,
                    task=task,
                    external_search=external_search,
                    target_time_window=plan.target_time_window,
                    as_of=as_of,
                )

            collected_tasks = await gather_cancel_on_error(
                *[run_task(task_index, task) for task_index, task in enumerate(tasks)]
            )
        return CollectedNews(tasks=collected_tasks)

    async def _collect_for_goal(
        self,
        *,
        task_index: int,
        task: ResearchTask,
        external_search: ExternalSearch,
        target_time_window: TargetTimeWindow | None,
        as_of: datetime,
    ) -> CollectedTask:
        internal_result, external_result = await _gather_two_branches(
            self._collect_internal(task_index=task_index, task=task),
            self._collect_external(
                task_index=task_index,
                task=task,
                external_search=external_search,
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
        internal_collection: TaskInternalCollectionStatus = (
            "failed" if internal_failed else "succeeded"
        )
        report = ResearchTaskReport(
            task_index=task_index,
            research_goal=task.research_goal,
            internal_collection=internal_collection,
            external_collection=external_status,
            generated_queries=generated_queries,
            provider_failed_query_count=provider_failed_query_count,
            internal_hit_count=len(hits),
            external_hit_count=len(external_hits),
        )
        return CollectedTask(
            task_index=task_index,
            research_goal=task.research_goal,
            internal_hits=hits,
            external_hits=external_hits,
            executed_queries=executed_queries,
            report=report,
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
        external_search: ExternalSearch,
        target_time_window: TargetTimeWindow | None,
        as_of: datetime,
    ) -> tuple[
        list[str],
        int,
        list[ExternalSearchHit],
        _ExternalBranchStatus,
        tuple[str, ...],
    ]:
        execution = await external_search.search(
            research_goal=task.research_goal,
            as_of=as_of,
            target_time_window=target_time_window,
            task_index=task_index,
        )
        generated_queries = list(execution.generated_queries)
        if not generated_queries:
            return [], 0, [], "query_generation_failed", ()
        await self._report_event(
            ExternalSearchQueriesGeneratedEvent(
                task_index=task_index,
                queries=generated_queries,
            )
        )
        failed_query_count = execution.provider_failed_query_count
        if failed_query_count == len(generated_queries):
            return generated_queries, failed_query_count, [], "provider_failed", ()

        await self._report_event(
            ExternalSearchHitsFetchedEvent(
                task_index=task_index,
                hit_count=len(execution.hits),
            )
        )
        return (
            generated_queries,
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
