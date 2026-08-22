"""Run単位のニュース収集。各research goalへ内部検索と外部検索を並列に行う。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import AsyncExitStack, asynccontextmanager
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
    TaskExternalCollectionStatus,
    TaskInternalCollectionStatus,
)
from app.agent.evidence_collection.external_search.agent import EXTERNAL_QUERY_AGENT
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearch,
    ExternalSearchDateFilter,
    ExternalSearchHit,
    ExternalSearchScopeFactory,
    TimeFilterFailureReason,
)
from app.agent.evidence_collection.external_search.observability import (
    observe_time_filter_resolution,
)
from app.agent.evidence_collection.external_search.policy import (
    resolve_external_search_agent_count,
)
from app.agent.evidence_collection.external_search.time_filter import (
    ExternalSearchDateFilterResolutionError,
    resolve_external_search_date_filter,
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
class _OpenedExternal:
    """collectのあいだだけ借りる外部検索と、期間解決の結果。"""

    search: ExternalSearch | None
    date_filter: ExternalSearchDateFilter | None
    time_filter_failure: TimeFilterFailureReason | None
    target_time_window: TargetTimeWindow | None


@dataclass(frozen=True, slots=True)
class EvidenceCollectionService:
    """Run単位でニュースを収集する。精査と回答生成は持たない。"""

    internal_search: InternalSearch
    external_search_scope_factory: ExternalSearchScopeFactory
    events: AnswerEventReporter | None = None
    requested_agent_count: int | None = None

    async def collect(
        self,
        *,
        plan: SearchPlan,
        as_of: datetime,
    ) -> CollectedNews:
        tasks = plan.research_tasks
        effective_agent_count = resolve_external_search_agent_count(
            task_count=len(tasks),
            requested_agent_count=self.requested_agent_count,
        )
        semaphore = asyncio.Semaphore(max(1, effective_agent_count))

        async with self._external_scope(plan=plan, as_of=as_of) as external:

            async def run_task(task_index: int, task: ResearchTask) -> CollectedTask:
                async with semaphore:
                    return await self._collect_for_goal(
                        task_index=task_index,
                        task=task,
                        external=external,
                        as_of=as_of,
                    )

            collected_tasks = await gather_cancel_on_error(
                *[run_task(task_index, task) for task_index, task in enumerate(tasks)]
            )
        return CollectedNews(
            tasks=collected_tasks,
            requested_agent_count=self.requested_agent_count,
            effective_agent_count=effective_agent_count,
        )

    @asynccontextmanager
    async def _external_scope(
        self,
        *,
        plan: SearchPlan,
        as_of: datetime,
    ) -> AsyncIterator[_OpenedExternal]:
        date_filter, time_filter_failure = _resolve_time_filter(plan=plan, as_of=as_of)
        async with AsyncExitStack() as scope:
            # 期間解決に失敗したRunは全taskで外部収集を行わないため資源を開かない。
            search: ExternalSearch | None = (
                None
                if time_filter_failure is not None
                else await scope.enter_async_context(
                    self.external_search_scope_factory()
                )
            )
            yield _OpenedExternal(
                search=search,
                date_filter=date_filter,
                time_filter_failure=time_filter_failure,
                target_time_window=plan.target_time_window,
            )

    async def _collect_for_goal(
        self,
        *,
        task_index: int,
        task: ResearchTask,
        external: _OpenedExternal,
        as_of: datetime,
    ) -> CollectedTask:
        internal_result, external_result = await _gather_two_branches(
            self._collect_internal(task_index=task_index, task=task),
            self._collect_external(
                task_index=task_index,
                task=task,
                external_search=external.search,
                date_filter=external.date_filter,
                target_time_window=external.target_time_window,
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
        external_collection, report_queries, report_failed_count = (
            _external_collection_fields(
                external_status=external_status,
                generated_queries=generated_queries,
                provider_failed_query_count=provider_failed_query_count,
                time_filter_failure=external.time_filter_failure,
            )
        )
        report = ResearchTaskReport(
            task_index=task_index,
            research_goal=task.research_goal,
            internal_collection=internal_collection,
            external_collection=external_collection,
            time_filter_failure_reason=external.time_filter_failure,
            generated_queries=report_queries,
            provider_failed_query_count=report_failed_count,
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
        external_search: ExternalSearch | None,
        date_filter: ExternalSearchDateFilter | None,
        target_time_window: TargetTimeWindow | None,
        as_of: datetime,
    ) -> tuple[
        list[str],
        int,
        list[ExternalSearchHit],
        _ExternalBranchStatus | None,
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


def _resolve_time_filter(
    *,
    plan: SearchPlan,
    as_of: datetime,
) -> tuple[ExternalSearchDateFilter | None, TimeFilterFailureReason | None]:
    tasks = plan.research_tasks
    try:
        date_filter = resolve_external_search_date_filter(
            plan.target_time_window,
            as_of=as_of,
        )
    except ExternalSearchDateFilterResolutionError as exc:
        observe_time_filter_resolution(
            result="failed",
            reason=exc.reason,
            task_count=len(tasks),
        )
        return None, exc.reason
    observe_time_filter_resolution(
        result="not_requested" if date_filter is None else "resolved",
        reason="none",
        task_count=len(tasks),
    )
    return date_filter, None


def _external_collection_fields(
    *,
    external_status: _ExternalBranchStatus | None,
    generated_queries: list[str],
    provider_failed_query_count: int,
    time_filter_failure: TimeFilterFailureReason | None,
) -> tuple[TaskExternalCollectionStatus, list[str], int]:
    """time filter失敗を含め、taskのexternal_collection診断を1箇所で導出する。"""
    if time_filter_failure is not None:
        return "time_filter_failed", [], 0
    # 外部scopeが有効な経路では外部枝が必ずstatusを返す。
    assert external_status is not None  # noqa: S101
    return (
        external_status,
        generated_queries,
        provider_failed_query_count,
    )


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
