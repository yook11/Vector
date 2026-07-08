"""Question plan retrieval service tests."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import ValidationError

from app.agent.external_search import ExternalSearchOutcome
from app.agent.internal_retrieval.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.agent.internal_retrieval.query_embedding import InternalSearchQueries
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    RetrievalPlan,
)
from app.agent.retrieval import QuestionPlanRetrievalService, RetrievalOutcome
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory


def _as_of() -> datetime:
    return datetime(2026, 7, 4, 9, 0, tzinfo=UTC)


def _plan(
    mode: Literal["internal", "external", "internal_and_external"],
    *,
    internal_queries: list[str] | None = None,
    target_time_window: str | None = None,
) -> RetrievalPlan:
    internal_queries = (
        internal_queries or ["NVIDIA AI GPU 直近動向"]
        if mode in {"internal", "internal_and_external"}
        else []
    )
    external_research_tasks = (
        [_external_task()] if mode in {"external", "internal_and_external"} else []
    )
    match mode:
        case "internal":
            return InternalRetrievalPlan(
                internal_queries=internal_queries,
                reason="test reason",
            )
        case "external":
            return ExternalSearchPlan(
                external_research_tasks=external_research_tasks,
                target_time_window=target_time_window,
                reason="test reason",
            )
        case "internal_and_external":
            return InternalAndExternalPlan(
                internal_queries=internal_queries,
                external_research_tasks=external_research_tasks,
                target_time_window=target_time_window,
                reason="test reason",
            )


def _external_task(
    collection_goal: str = "NVIDIA のAI GPU最新根拠を集める",
) -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=collection_goal)


def _external_outcome() -> ExternalSearchOutcome:
    return ExternalSearchOutcome(
        tasks=[],
        evidence=[],
        task_reports=[],
        effective_agent_count=0,
    )


def _hit(*, curation_id: int, title: str, distance: float) -> InternalArticleSearchHit:
    article = InScopeAnalyzedArticle(
        curation_id=curation_id,
        title=title,
        summary=f"{title} summary",
        assessment_result=InScope(
            category=InScopeCategory.AI,
            investor_take="投資家視点",
            key_points=[],
        ),
    )
    return InternalArticleSearchHit(
        assessment_id=curation_id + 1000,
        article=article,
        content=InternalArticleContent.from_article(article, published_at=None),
        distance=distance,
    )


class FakeInternalArticleRetriever:
    def __init__(
        self,
        *,
        hits: Sequence[InternalArticleSearchHit] = (),
        error: Exception | None = None,
        started_event: asyncio.Event | None = None,
        wait_for_event: asyncio.Event | None = None,
        about_to_raise_event: asyncio.Event | None = None,
    ) -> None:
        self._hits = list(hits)
        self._error = error
        self._started_event = started_event
        self._wait_for_event = wait_for_event
        self._about_to_raise_event = about_to_raise_event
        self.calls: list[InternalSearchQueries] = []
        self.completed = False

    async def search_articles(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalArticleSearchHit]:
        self.calls.append(queries)
        if self._started_event is not None:
            self._started_event.set()
        if self._wait_for_event is not None:
            await self._wait_for_event.wait()
        if self._error is not None:
            if self._about_to_raise_event is not None:
                self._about_to_raise_event.set()
            raise self._error
        self.completed = True
        return list(self._hits)


class FakeExternalPlanSearcher:
    def __init__(
        self,
        outcome: ExternalSearchOutcome | None = None,
        *,
        error: Exception | None = None,
        started_event: asyncio.Event | None = None,
        wait_for_event: asyncio.Event | None = None,
        about_to_raise_event: asyncio.Event | None = None,
    ) -> None:
        self._outcome = outcome or _external_outcome()
        self._error = error
        self._started_event = started_event
        self._wait_for_event = wait_for_event
        self._about_to_raise_event = about_to_raise_event
        self.calls: list[
            tuple[list[ExternalResearchTask], str | None, datetime, int | None]
        ] = []
        self.completed = False

    async def search(
        self,
        external_research_tasks: list[ExternalResearchTask],
        *,
        target_time_window: str | None,
        as_of: datetime,
        requested_agent_count: int | None = None,
    ) -> ExternalSearchOutcome:
        self.calls.append(
            (external_research_tasks, target_time_window, as_of, requested_agent_count)
        )
        if self._started_event is not None:
            self._started_event.set()
        if self._wait_for_event is not None:
            await self._wait_for_event.wait()
        if self._error is not None:
            if self._about_to_raise_event is not None:
                self._about_to_raise_event.set()
            raise self._error
        self.completed = True
        return self._outcome


class InternalSearchBoom(Exception):
    pass


class ExternalSearchBoom(Exception):
    pass


async def _cancel_if_pending(task: asyncio.Task[object]) -> None:
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_retrieve_internal_preserves_search_hit_order() -> None:
    plan = _plan("internal")
    # distance 降順で返す fake でも、search の返却順がそのまま保持される。
    hits = [
        _hit(curation_id=1, title="OpenAI", distance=0.5),
        _hit(curation_id=2, title="NVIDIA", distance=0.1),
    ]
    internal_search = FakeInternalArticleRetriever(hits=hits)
    service = QuestionPlanRetrievalService(internal_search=internal_search)

    outcome = await service.retrieve(plan, as_of=_as_of())

    assert (
        internal_search.calls
        == [InternalSearchQueries(queries=("NVIDIA AI GPU 直近動向",))]
        and outcome.internal_hits == hits
        and outcome.unmet_requirements == []
    )


@pytest.mark.asyncio
async def test_retrieve_internal_does_not_call_external_search() -> None:
    plan = _plan("internal")
    internal_search = FakeInternalArticleRetriever()
    external_search = FakeExternalPlanSearcher()
    service = QuestionPlanRetrievalService(
        internal_search=internal_search,
        external_search=external_search,
    )

    await service.retrieve(plan, as_of=_as_of())

    assert external_search.calls == []


@pytest.mark.asyncio
async def test_retrieve_internal_passes_plan_queries_directly_to_leaf_search() -> None:
    plan = _plan(
        "internal",
        internal_queries=["NVIDIA", "nvidia", "OpenAI"],
    )
    internal_search = FakeInternalArticleRetriever()
    service = QuestionPlanRetrievalService(internal_search=internal_search)

    await service.retrieve(plan, as_of=_as_of())

    assert internal_search.calls == [
        InternalSearchQueries(queries=("NVIDIA", "nvidia", "OpenAI"))
    ]


@pytest.mark.asyncio
async def test_retrieve_external_skips_internal_search_and_records_unmet() -> None:
    internal_search = FakeInternalArticleRetriever()
    service = QuestionPlanRetrievalService(internal_search=internal_search)

    outcome = await service.retrieve(_plan("external"), as_of=_as_of())

    assert (
        internal_search.calls == []
        and outcome.internal_hits == []
        and outcome.external_search is None
        and outcome.unmet_requirements == ["external_search"]
    )


@pytest.mark.asyncio
async def test_retrieve_external_runs_external_search_when_available() -> None:
    plan = _plan("external")
    internal_search = FakeInternalArticleRetriever()
    external_search = FakeExternalPlanSearcher()
    service = QuestionPlanRetrievalService(
        internal_search=internal_search,
        external_search=external_search,
        requested_external_agent_count=4,
    )

    outcome = await service.retrieve(plan, as_of=_as_of())

    assert (
        internal_search.calls == []
        and external_search.calls == [([_external_task()], None, _as_of(), 4)]
        and outcome.external_search == external_search._outcome
        and outcome.unmet_requirements == []
    )


@pytest.mark.asyncio
async def test_retrieve_internal_and_external_runs_internal_and_records_unmet() -> None:
    plan = _plan("internal_and_external")
    hits = [_hit(curation_id=1, title="NVIDIA", distance=0.1)]
    internal_search = FakeInternalArticleRetriever(hits=hits)
    service = QuestionPlanRetrievalService(internal_search=internal_search)

    outcome = await service.retrieve(plan, as_of=_as_of())

    assert (
        internal_search.calls
        == [InternalSearchQueries(queries=("NVIDIA AI GPU 直近動向",))]
        and outcome.internal_hits == hits
        and outcome.external_search is None
        and outcome.unmet_requirements == ["external_search"]
    )


@pytest.mark.asyncio
async def test_retrieve_internal_and_external_runs_both_retrievals() -> None:
    plan = _plan("internal_and_external")
    hits = [_hit(curation_id=1, title="NVIDIA", distance=0.1)]
    internal_search = FakeInternalArticleRetriever(hits=hits)
    external_search = FakeExternalPlanSearcher()
    service = QuestionPlanRetrievalService(
        internal_search=internal_search,
        external_search=external_search,
    )

    outcome = await service.retrieve(plan, as_of=_as_of())

    assert (
        internal_search.calls
        == [InternalSearchQueries(queries=("NVIDIA AI GPU 直近動向",))]
        and external_search.calls == [([_external_task()], None, _as_of(), None)]
        and outcome.internal_hits == hits
        and outcome.external_search == external_search._outcome
        and outcome.unmet_requirements == []
    )


@pytest.mark.asyncio
async def test_retrieve_internal_and_external_retrievals_overlap() -> None:
    plan = _plan("internal_and_external")
    hits = [_hit(curation_id=1, title="NVIDIA", distance=0.1)]
    internal_started = asyncio.Event()
    external_started = asyncio.Event()
    internal_search = FakeInternalArticleRetriever(
        hits=hits,
        started_event=internal_started,
        wait_for_event=external_started,
    )
    external_search = FakeExternalPlanSearcher(
        started_event=external_started,
        wait_for_event=internal_started,
    )
    service = QuestionPlanRetrievalService(
        internal_search=internal_search,
        external_search=external_search,
    )

    outcome = await asyncio.wait_for(
        service.retrieve(plan, as_of=_as_of()),
        timeout=0.5,
    )

    assert (
        internal_started.is_set()
        and external_started.is_set()
        and outcome.internal_hits == hits
        and outcome.external_search == external_search._outcome
        and outcome.unmet_requirements == []
    )


@pytest.mark.asyncio
async def test_retrieve_waits_for_external_on_internal_error() -> None:
    plan = _plan("internal_and_external")
    external_started = asyncio.Event()
    external_may_complete = asyncio.Event()
    internal_about_to_raise = asyncio.Event()
    internal_search = FakeInternalArticleRetriever(
        error=InternalSearchBoom("internal search failed"),
        wait_for_event=external_started,
        about_to_raise_event=internal_about_to_raise,
    )
    external_search = FakeExternalPlanSearcher(
        started_event=external_started,
        wait_for_event=external_may_complete,
    )
    service = QuestionPlanRetrievalService(
        internal_search=internal_search,
        external_search=external_search,
    )
    retrieve_task = asyncio.create_task(service.retrieve(plan, as_of=_as_of()))

    try:
        await asyncio.wait_for(internal_about_to_raise.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert not retrieve_task.done()

        external_may_complete.set()
        with pytest.raises(InternalSearchBoom, match="internal search failed"):
            await asyncio.wait_for(retrieve_task, timeout=0.5)

        assert external_search.completed
    finally:
        await _cancel_if_pending(retrieve_task)


@pytest.mark.asyncio
async def test_retrieve_waits_for_internal_on_external_error() -> None:
    plan = _plan("internal_and_external")
    hits = [_hit(curation_id=1, title="NVIDIA", distance=0.1)]
    internal_started = asyncio.Event()
    internal_may_complete = asyncio.Event()
    external_about_to_raise = asyncio.Event()
    internal_search = FakeInternalArticleRetriever(
        hits=hits,
        started_event=internal_started,
        wait_for_event=internal_may_complete,
    )
    external_search = FakeExternalPlanSearcher(
        error=ExternalSearchBoom("external search failed"),
        wait_for_event=internal_started,
        about_to_raise_event=external_about_to_raise,
    )
    service = QuestionPlanRetrievalService(
        internal_search=internal_search,
        external_search=external_search,
    )
    retrieve_task = asyncio.create_task(service.retrieve(plan, as_of=_as_of()))

    try:
        await asyncio.wait_for(external_about_to_raise.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert not retrieve_task.done()

        internal_may_complete.set()
        with pytest.raises(ExternalSearchBoom, match="external search failed"):
            await asyncio.wait_for(retrieve_task, timeout=0.5)

        assert internal_search.completed
    finally:
        await _cancel_if_pending(retrieve_task)


@pytest.mark.asyncio
async def test_retrieve_internal_and_external_prefers_internal_error() -> None:
    plan = _plan("internal_and_external")
    internal_started = asyncio.Event()
    external_started = asyncio.Event()
    internal_search = FakeInternalArticleRetriever(
        error=InternalSearchBoom("internal search failed"),
        started_event=internal_started,
        wait_for_event=external_started,
    )
    external_search = FakeExternalPlanSearcher(
        error=ExternalSearchBoom("external search failed"),
        started_event=external_started,
        wait_for_event=internal_started,
    )
    service = QuestionPlanRetrievalService(
        internal_search=internal_search,
        external_search=external_search,
    )

    with pytest.raises(InternalSearchBoom, match="internal search failed"):
        await asyncio.wait_for(service.retrieve(plan, as_of=_as_of()), timeout=0.5)


@pytest.mark.asyncio
async def test_retrieve_propagates_internal_search_exception() -> None:
    service = QuestionPlanRetrievalService(
        internal_search=FakeInternalArticleRetriever(
            error=RuntimeError("internal search failed"),
        ),
    )

    with pytest.raises(RuntimeError, match="internal search failed"):
        await service.retrieve(_plan("internal"), as_of=_as_of())


def test_retrieval_outcome_rejects_external_search_and_external_unmet() -> None:
    with pytest.raises(ValidationError):
        RetrievalOutcome(
            external_search=_external_outcome(),
            unmet_requirements=["external_search"],
        )


def test_retrieval_outcome_allows_external_unmet_when_search_is_absent() -> None:
    outcome = RetrievalOutcome(unmet_requirements=["external_search"])

    assert outcome.external_search is None
    assert outcome.unmet_requirements == ["external_search"]
