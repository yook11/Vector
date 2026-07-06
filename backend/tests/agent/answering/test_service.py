"""Question answering service tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.answering.service import QuestionPlanRetrievalService, RetrievalOutcome
from app.agent.contract import ExternalResearchTask, QuestionPlan, RetrievalMode
from app.agent.external_search import ExternalSearchOutcome
from app.agent.internal_retrieval.article_search import (
    InternalArticleContent,
    InternalArticleSearchHit,
)
from app.analysis.analyzed_article import InScopeAnalyzedArticle
from app.analysis.assessment.domain.result import InScope, InScopeCategory


def _as_of() -> datetime:
    return datetime(2026, 7, 4, 9, 0, tzinfo=UTC)


def _plan(mode: RetrievalMode) -> QuestionPlan:
    internal_queries = (
        ["NVIDIA AI GPU 直近動向"]
        if mode in {"internal", "internal_and_external"}
        else []
    )
    external_research_tasks = (
        [_external_task()] if mode in {"external", "internal_and_external"} else []
    )
    return QuestionPlan(
        retrieval_mode=mode,
        internal_queries=internal_queries,
        external_research_tasks=external_research_tasks,
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
    ) -> None:
        self._hits = list(hits)
        self._error = error
        self.calls: list[QuestionPlan] = []

    async def search_plan_articles(
        self,
        plan: QuestionPlan,
    ) -> list[InternalArticleSearchHit]:
        self.calls.append(plan)
        if self._error is not None:
            raise self._error
        return list(self._hits)


class FakeExternalPlanSearcher:
    def __init__(self, outcome: ExternalSearchOutcome | None = None) -> None:
        self._outcome = outcome or _external_outcome()
        self.calls: list[tuple[QuestionPlan, datetime, int | None]] = []

    async def search_plan(
        self,
        plan: QuestionPlan,
        *,
        as_of: datetime,
        requested_agent_count: int | None = None,
    ) -> ExternalSearchOutcome:
        self.calls.append((plan, as_of, requested_agent_count))
        return self._outcome


@pytest.mark.asyncio
async def test_retrieve_none_skips_internal_search_and_returns_empty_outcome() -> None:
    internal_search = FakeInternalArticleRetriever()
    service = QuestionPlanRetrievalService(internal_search=internal_search)

    outcome = await service.retrieve(_plan("none"), as_of=_as_of())

    assert (
        internal_search.calls == []
        and outcome.internal_hits == []
        and outcome.unmet_requirements == []
    )


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
        internal_search.calls == [plan]
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
        and external_search.calls == [(plan, _as_of(), 4)]
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
        internal_search.calls == [plan]
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
        internal_search.calls == [plan]
        and external_search.calls == [(plan, _as_of(), None)]
        and outcome.internal_hits == hits
        and outcome.external_search == external_search._outcome
        and outcome.unmet_requirements == []
    )


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
