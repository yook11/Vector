"""External searchのquery生成とprovider実行を束ねるservice。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

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
from app.agent.evidence_collection.external_search.policy import (
    PROVIDER_SEARCH_TIMEOUT_SECONDS,
    QUERY_GENERATE_TIMEOUT_SECONDS,
    build_hit_pool,
    clean_generated_queries,
)
from app.agent.planning.contract import ExternalResearchTask, TargetTimeWindow
from app.agent.runtime.contract import AgentResponseInvalidError, AgentRuntime
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["ExternalSearchService"]


@dataclass(frozen=True, slots=True)
class ExternalSearchService:
    """External search port (`ExternalSearch`) の実装。"""

    query_runtime: AgentRuntime
    search_gateway: ExternalSearchGateway

    async def generate_queries(
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

    async def search_queries(
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
