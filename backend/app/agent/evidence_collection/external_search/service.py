"""External searchのquery生成とprovider実行を束ねるservice。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from app.agent.concurrency import gather_cancel_on_error
from app.agent.evidence_collection.external_search.agent import EXTERNAL_QUERY_AGENT
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_HITS_PER_QUERY,
    ExternalQueryGenerationInput,
    ExternalSearchDateFilter,
    ExternalSearchExecution,
    ExternalSearchGateway,
    ExternalSearchHit,
    ExternalSearchProviderError,
    ExternalSearchRequest,
)
from app.agent.evidence_collection.external_search.observability import (
    observe_time_filter_resolution,
)
from app.agent.evidence_collection.external_search.policy import (
    PROVIDER_SEARCH_TIMEOUT_SECONDS,
    QUERY_GENERATE_TIMEOUT_SECONDS,
    build_hit_pool,
    clean_generated_queries,
)
from app.agent.evidence_collection.external_search.time_filter import (
    ExternalSearchDateFilterResolutionError,
    resolve_external_search_date_filter,
)
from app.agent.phase_span import agent_phase
from app.agent.planning.contract import ExternalResearchTask, TargetTimeWindow
from app.agent.recording.external_search import (
    ExternalSearchRecorder,
    logfire_external_search_recorder,
)
from app.agent.runtime.contract import AgentResponseInvalidError, AgentRuntime
from app.analysis.ai_provider_errors import AIProviderError

if TYPE_CHECKING:
    from app.agent.evidence_collection.contract import TaskExternalCollectionStatus

__all__ = ["ExternalSearchService"]


@dataclass
class _PublicationFilterMemo:
    """1 scopeで期間解決を1回にするためのメモ。"""

    resolved: bool = False
    date_filter: ExternalSearchDateFilter | None = None


@dataclass(frozen=True, slots=True)
class ExternalSearchService:
    """External search port (`ExternalSearch`) の実装。"""

    query_runtime: AgentRuntime
    search_gateway: ExternalSearchGateway
    _filter_memo: _PublicationFilterMemo = field(default_factory=_PublicationFilterMemo)
    _resolve_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    recorder: ExternalSearchRecorder = logfire_external_search_recorder

    async def search(
        self,
        *,
        research_goal: str,
        as_of: datetime,
        target_time_window: TargetTimeWindow | None,
        task_index: int,
    ) -> ExternalSearchExecution:
        call = await self.recorder.start()
        try:
            date_filter = await self._publication_filter(
                target_time_window=target_time_window,
                as_of=as_of,
            )
            with agent_phase(
                phase="evidence_collection",
                agent_name=EXTERNAL_QUERY_AGENT.name,
                task_index=task_index,
            ):
                queries = await self._generate_queries(
                    research_goal=research_goal,
                    as_of=as_of,
                    target_time_window=target_time_window,
                )
            if not queries:
                execution = ExternalSearchExecution(
                    generated_queries=(),
                    hits=[],
                    provider_failed_query_count=0,
                    executed_queries=(),
                )
            else:
                execution = await self._search_queries(queries, date_filter=date_filter)
            await self.recorder.end(
                call,
                outcome=_outcome_from_execution(execution),
            )
            return execution
        except (asyncio.CancelledError, GeneratorExit):
            await self.recorder.end(call, stopped=True)
            raise
        except Exception:
            await self.recorder.end(call)
            raise

    async def _publication_filter(
        self,
        *,
        target_time_window: TargetTimeWindow | None,
        as_of: datetime,
    ) -> ExternalSearchDateFilter | None:
        async with self._resolve_lock:
            if self._filter_memo.resolved:
                return self._filter_memo.date_filter
            try:
                date_filter = resolve_external_search_date_filter(
                    target_time_window,
                    as_of=as_of,
                )
            except ExternalSearchDateFilterResolutionError as exc:
                observe_time_filter_resolution(result="failed", reason=exc.reason)
                self._filter_memo.date_filter = None
                self._filter_memo.resolved = True
                return None
            observe_time_filter_resolution(
                result="not_requested" if date_filter is None else "resolved",
                reason="none",
            )
            self._filter_memo.date_filter = date_filter
            self._filter_memo.resolved = True
            return date_filter

    async def _generate_queries(
        self,
        *,
        research_goal: str,
        as_of: datetime,
        target_time_window: TargetTimeWindow | None,
    ) -> list[str]:
        """生成に失敗した場合も空listを返し、呼び出し側の失敗分類に委ねる。"""
        query_input = ExternalQueryGenerationInput(
            task=ExternalResearchTask(research_goal=research_goal),
            as_of=as_of,
            target_time_window=target_time_window,
        )
        try:
            draft = await asyncio.wait_for(
                self.query_runtime.call(
                    EXTERNAL_QUERY_AGENT,
                    query_input,
                    attempt_number=1,
                ),
                timeout=QUERY_GENERATE_TIMEOUT_SECONDS,
            )
        except (AgentResponseInvalidError, AIProviderError, TimeoutError):
            return []
        return clean_generated_queries(draft.queries)

    async def _search_queries(
        self,
        queries: list[str],
        *,
        date_filter: ExternalSearchDateFilter | None,
    ) -> ExternalSearchExecution:
        hits_by_query: list[list[ExternalSearchHit]] = []
        executed_queries: list[str] = []
        provider_failed_query_count = 0
        # gather_cancel_on_errorはasyncio.gatherに委譲しており、結果順は
        # 完了順でなく渡したawaitablesの順(=queriesの順)と一致する。
        provider_results = await gather_cancel_on_error(
            *[self._search_query(query, date_filter=date_filter) for query in queries]
        )
        for query, (hits, failed) in zip(queries, provider_results, strict=True):
            if failed:
                provider_failed_query_count += 1
                hits_by_query.append([])
                continue
            hits_by_query.append(hits)
            executed_queries.append(query)

        return ExternalSearchExecution(
            generated_queries=tuple(queries),
            hits=build_hit_pool(hits_by_query),
            provider_failed_query_count=provider_failed_query_count,
            executed_queries=tuple(executed_queries),
        )

    async def _search_query(
        self,
        query: str,
        *,
        date_filter: ExternalSearchDateFilter | None,
    ) -> tuple[list[ExternalSearchHit], bool]:
        try:
            hits = await asyncio.wait_for(
                self.search_gateway.search(
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


def _outcome_from_execution(
    execution: ExternalSearchExecution,
) -> TaskExternalCollectionStatus:
    if not execution.generated_queries:
        return "query_generation_failed"
    if execution.provider_failed_query_count == len(execution.generated_queries):
        return "provider_failed"
    return "succeeded"
