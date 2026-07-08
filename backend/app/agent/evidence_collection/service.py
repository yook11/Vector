"""Evidence collection service."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import assert_never

from app.agent.evidence_collection.contract import (
    EvidenceCollectionOutcome,
    ExternalPlanSearcher,
    InternalArticleRetriever,
)
from app.agent.external_search import ExternalSearchOutcome
from app.agent.internal_retrieval.query_embedding import InternalSearchQueries
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    RetrievalPlan,
)

__all__ = ["EvidenceCollectionService"]


class EvidenceCollectionService:
    """RetrievalPlan を読んで internal/external の evidence 収集を起動する工程。"""

    def __init__(
        self,
        *,
        internal_search: InternalArticleRetriever,
        external_search: ExternalPlanSearcher | None = None,
        requested_external_agent_count: int | None = None,
    ) -> None:
        self._internal_search = internal_search
        self._external_search = external_search
        self._requested_external_agent_count = requested_external_agent_count

    async def collect(
        self,
        plan: RetrievalPlan,
        *,
        as_of: datetime,
    ) -> EvidenceCollectionOutcome:
        match plan:
            case InternalRetrievalPlan(internal_queries=internal_queries):
                hits = await self._internal_search.search_articles(
                    InternalSearchQueries(queries=tuple(internal_queries))
                )
                return EvidenceCollectionOutcome(internal_hits=hits)
            case ExternalSearchPlan(
                external_research_tasks=external_research_tasks,
                target_time_window=target_time_window,
            ):
                external = await self._search_external(
                    external_research_tasks,
                    target_time_window=target_time_window,
                    as_of=as_of,
                )
                if external is not None:
                    return EvidenceCollectionOutcome(external_search=external)
                return EvidenceCollectionOutcome(
                    unmet_requirements=["external_search"],
                )
            case InternalAndExternalPlan(
                internal_queries=internal_queries,
                external_research_tasks=external_research_tasks,
                target_time_window=target_time_window,
            ):
                internal_search_queries = InternalSearchQueries(
                    queries=tuple(internal_queries)
                )
                if self._external_search is None:
                    hits = await self._internal_search.search_articles(
                        internal_search_queries
                    )
                    return EvidenceCollectionOutcome(
                        internal_hits=hits,
                        unmet_requirements=["external_search"],
                    )

                hits_result, external_result = await asyncio.gather(
                    self._internal_search.search_articles(internal_search_queries),
                    self._search_external(
                        external_research_tasks,
                        target_time_window=target_time_window,
                        as_of=as_of,
                    ),
                    return_exceptions=True,
                )
                hits = _raise_if_exception(hits_result)
                external = _raise_if_exception(external_result)
                if external is not None:
                    return EvidenceCollectionOutcome(
                        internal_hits=hits,
                        external_search=external,
                    )
                return EvidenceCollectionOutcome(
                    internal_hits=hits,
                    unmet_requirements=["external_search"],
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def _search_external(
        self,
        external_research_tasks: list[ExternalResearchTask],
        *,
        target_time_window: str | None,
        as_of: datetime,
    ) -> ExternalSearchOutcome | None:
        if self._external_search is None:
            return None
        return await self._external_search.search(
            external_research_tasks,
            target_time_window=target_time_window,
            as_of=as_of,
            requested_agent_count=self._requested_external_agent_count,
        )


def _raise_if_exception[ResultT](result: ResultT | BaseException) -> ResultT:
    if isinstance(result, BaseException):
        raise result
    return result
