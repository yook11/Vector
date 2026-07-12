"""Evidence collection service."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import assert_never

from app.agent.contract import EvidenceCollectionFailure
from app.agent.evidence_collection.contract import (
    EvidenceCollectionOutcome,
    ExternalPlanSearcher,
    InternalArticleRetriever,
)
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalSearchError,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
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
                hits, internal_failed = await self._collect_internal(
                    InternalSearchQueries(queries=tuple(internal_queries))
                )
                return EvidenceCollectionOutcome(
                    internal_hits=hits,
                    collection_failures=(
                        ["internal_search"] if internal_failed else []
                    ),
                )
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
                    collection_failures=["external_search"],
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
                    hits, internal_failed = await self._collect_internal(
                        internal_search_queries
                    )
                    collection_failures: list[EvidenceCollectionFailure] = []
                    if internal_failed:
                        collection_failures.append("internal_search")
                    collection_failures.append("external_search")
                    return EvidenceCollectionOutcome(
                        internal_hits=hits,
                        collection_failures=collection_failures,
                    )

                internal_result, external_result = await asyncio.gather(
                    self._collect_internal(internal_search_queries),
                    self._search_external(
                        external_research_tasks,
                        target_time_window=target_time_window,
                        as_of=as_of,
                    ),
                    return_exceptions=True,
                )
                hits, internal_failed = _raise_if_exception(internal_result)
                external = _raise_if_exception(external_result)
                collection_failures: list[EvidenceCollectionFailure] = (
                    ["internal_search"] if internal_failed else []
                )
                if external is not None:
                    return EvidenceCollectionOutcome(
                        internal_hits=hits,
                        external_search=external,
                        collection_failures=collection_failures,
                    )
                collection_failures.append("external_search")
                return EvidenceCollectionOutcome(
                    internal_hits=hits,
                    collection_failures=collection_failures,
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

    async def _collect_internal(
        self,
        queries: InternalSearchQueries,
    ) -> tuple[list[InternalArticleSearchHit], bool]:
        try:
            return await self._internal_search.search_articles(queries), False
        except InternalSearchError:
            return [], True


def _raise_if_exception[ResultT](result: ResultT | BaseException) -> ResultT:
    if isinstance(result, BaseException):
        raise result
    return result
