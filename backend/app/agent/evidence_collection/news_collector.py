"""Run単位のニュース収集。全research taskへResearcherを並列にfan-outする。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime

from app.agent.concurrency import gather_cancel_on_error
from app.agent.evidence_collection.contract import (
    CollectedNews,
    CollectedTask,
    ResearchTaskReport,
    TaskExternalCollectionStatus,
    TaskInternalCollectionStatus,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearch,
    ExternalSearchDateFilter,
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
from app.agent.evidence_collection.researcher import Researcher, ResearchTaskHits
from app.agent.planning.contract import ResearchTask, SearchPlan

__all__ = ["NewsCollector"]


@dataclass(frozen=True, slots=True)
class NewsCollector:
    """Run単位でニュースを収集する。精査と回答生成は持たない。"""

    researcher: Researcher
    external_search_scope_factory: ExternalSearchScopeFactory
    requested_agent_count: int | None = None

    def _resolve_time_filter(
        self,
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

    async def collect(
        self,
        *,
        plan: SearchPlan,
        as_of: datetime,
    ) -> CollectedNews:
        date_filter, time_filter_failure = self._resolve_time_filter(
            plan=plan,
            as_of=as_of,
        )
        tasks = plan.research_tasks
        effective_agent_count = resolve_external_search_agent_count(
            task_count=len(tasks),
            requested_agent_count=self.requested_agent_count,
        )
        semaphore = asyncio.Semaphore(max(1, effective_agent_count))

        async with AsyncExitStack() as scope:
            # 期間解決に失敗したRunは全taskで外部収集を行わないため資源を開かない。
            external_search: ExternalSearch | None = (
                None
                if time_filter_failure is not None
                else await scope.enter_async_context(
                    self.external_search_scope_factory()
                )
            )

            async def run_task(task_index: int, task: ResearchTask) -> CollectedTask:
                async with semaphore:
                    return await self._collect_task(
                        task_index=task_index,
                        task=task,
                        external_search=external_search,
                        date_filter=date_filter,
                        time_filter_failure=time_filter_failure,
                        plan=plan,
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

    async def _collect_task(
        self,
        *,
        task_index: int,
        task: ResearchTask,
        external_search: ExternalSearch | None,
        date_filter: ExternalSearchDateFilter | None,
        time_filter_failure: TimeFilterFailureReason | None,
        plan: SearchPlan,
        as_of: datetime,
    ) -> CollectedTask:
        collected = await self.researcher.collect(
            task_index=task_index,
            task=task,
            external_search=external_search,
            date_filter=date_filter,
            target_time_window=plan.target_time_window,
            as_of=as_of,
        )
        internal_collection: TaskInternalCollectionStatus = (
            "failed" if collected.internal_failed else "succeeded"
        )
        external_collection, generated_queries, provider_failed_query_count = (
            _external_collection_fields(
                collected=collected,
                time_filter_failure=time_filter_failure,
            )
        )
        report = ResearchTaskReport(
            task_index=task_index,
            research_goal=task.research_goal,
            internal_collection=internal_collection,
            external_collection=external_collection,
            time_filter_failure_reason=time_filter_failure,
            generated_queries=generated_queries,
            provider_failed_query_count=provider_failed_query_count,
            internal_hit_count=len(collected.internal_hits),
            external_hit_count=len(collected.external_hits),
        )
        return CollectedTask(
            task_index=task_index,
            research_goal=task.research_goal,
            internal_hits=collected.internal_hits,
            external_hits=collected.external_hits,
            executed_queries=collected.executed_queries,
            report=report,
        )


def _external_collection_fields(
    *,
    collected: ResearchTaskHits,
    time_filter_failure: TimeFilterFailureReason | None,
) -> tuple[TaskExternalCollectionStatus, list[str], int]:
    """time filter失敗を含め、taskのexternal_collection診断3値を1箇所で導出する。"""
    if time_filter_failure is not None:
        return "time_filter_failed", [], 0
    external_status = collected.external_status
    # time filter失敗以外の経路ではscopeが常に有効で、Researcherは必ずstatusを返す。
    assert external_status is not None  # noqa: S101
    return (
        external_status,
        collected.generated_queries,
        collected.provider_failed_query_count,
    )
