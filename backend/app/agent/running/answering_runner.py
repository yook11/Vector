"""1回の回答処理を進行する AnsweringRunner。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import assert_never
from uuid import UUID

import logfire

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.evidence import (
    normalize_answer_evidence,
)
from app.agent.answering.result_assembly import assemble_evidence_result
from app.agent.contract import (
    AnswerGenerationStopped,
    AnswerProgressReporter,
    AnswerProgressStage,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
)
from app.agent.evidence_collection.contract import EvidenceCollectionOutcome
from app.agent.evidence_collection.external_search import ExternalSearchOutcome
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import InternalSearchError
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalSearchQueries,
)
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    InternalAndExternalPlan,
    InternalRetrievalPlan,
    NoRetrievalPlan,
    PlanningRequest,
    RetrievalPlan,
)
from app.agent.running.contract import (
    AnsweringPhases,
    AnsweringPhasesFactory,
    AnsweringRunContext,
    QuestionContextPreparer,
    RunContext,
    RunHooks,
    RunInput,
    RunResult,
)
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = ["AnsweringRunner"]

_SPAN_NAME = "agent_answering_run"


class AnsweringRunner:
    def __init__(
        self,
        *,
        context_preparer: QuestionContextPreparer,
        phases_factory: AnsweringPhasesFactory,
        progress: AnswerProgressReporter | None = None,
    ) -> None:
        self._context_preparer = context_preparer
        self._phases_factory = phases_factory
        self._progress = progress

    async def run(
        self,
        input: RunInput,
        *,
        run_context: RunContext,
        hooks: RunHooks | None = None,
    ) -> RunResult:
        with _answering_run_span(run_id=run_context.run_id):
            preparation = await self._context_preparer.prepare(
                question=input.question,
                history=list(input.history),
                as_of=run_context.as_of,
                run_id=run_context.run_id,
            )
            answering_context = AnsweringRunContext(
                run_context=run_context,
                question_context=preparation.context,
                previous_answer=_latest_assistant_answer(input.history),
            )
            if hooks is not None:
                await hooks.on_answering_context_prepared(
                    original_question=input.question,
                    has_history=bool(input.history),
                    question_context=answering_context.question_context,
                )
            phases = self._phases_factory()

            await self._report_progress("planning")
            planning_request = PlanningRequest(
                context=answering_context.question_context,
                as_of=answering_context.run_context.as_of,
            )
            answering_request = AnsweringRequest(
                context=answering_context.question_context,
                as_of=answering_context.run_context.as_of,
            )
            plan = await phases.planner.plan(planning_request)
            match plan:
                case NoRetrievalPlan():
                    final_output = await self._answer_directly(
                        phases=phases,
                        request=answering_request,
                        previous_answer=answering_context.previous_answer,
                    )
                case (
                    InternalRetrievalPlan()
                    | ExternalSearchPlan()
                    | InternalAndExternalPlan()
                ):
                    final_output = await self._answer_with_evidence(
                        phases=phases,
                        request=answering_request,
                        plan=plan,
                    )
                case _ as unreachable:
                    assert_never(unreachable)
            return RunResult(
                final_output=final_output,
                context=answering_context,
            )

    async def _answer_directly(
        self,
        *,
        phases: AnsweringPhases,
        request: AnsweringRequest,
        previous_answer: str,
    ) -> AnswerQuestionResult:
        await self._report_progress("synthesizing")
        draft = await phases.direct_answerer.answer(
            request=request,
            previous_answer=previous_answer,
        )
        return AnswerQuestionResult(
            status="answered",
            answer=draft.answer,
            sources=[],
            missing_aspects=[],
            retrieval=AnswerRetrievalSummary(
                planned_mode="none",
                collection_failures=[],
            ),
        )

    async def _answer_with_evidence(
        self,
        *,
        phases: AnsweringPhases,
        request: AnsweringRequest,
        plan: RetrievalPlan,
    ) -> AnswerQuestionResult:
        await self._report_progress("retrieving")
        outcome = await self._collect_evidence(
            phases=phases,
            plan=plan,
            as_of=request.as_of,
        )
        evidence = normalize_answer_evidence(outcome)

        await self._report_progress("synthesizing")
        draft = await phases.evidence_answerer.answer(
            request=request,
            evidence=evidence,
            target_time_window=_plan_target_time_window(plan),
        )
        return assemble_evidence_result(
            context=request.context,
            plan=plan,
            outcome=outcome,
            evidence=evidence,
            draft=draft,
        )

    async def _collect_evidence(
        self,
        *,
        phases: AnsweringPhases,
        plan: RetrievalPlan,
        as_of: datetime,
    ) -> EvidenceCollectionOutcome:
        match plan:
            case InternalRetrievalPlan(internal_queries=internal_queries):
                hits, internal_failed = await self._collect_internal(
                    phases=phases,
                    queries=InternalSearchQueries(queries=tuple(internal_queries)),
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
                external = await self._collect_external(
                    phases=phases,
                    tasks=external_research_tasks,
                    target_time_window=target_time_window,
                    as_of=as_of,
                )
                return EvidenceCollectionOutcome(external_search=external)
            case InternalAndExternalPlan(
                internal_queries=internal_queries,
                external_research_tasks=external_research_tasks,
                target_time_window=target_time_window,
            ):
                internal_result, external_result = await asyncio.gather(
                    self._collect_internal(
                        phases=phases,
                        queries=InternalSearchQueries(queries=tuple(internal_queries)),
                    ),
                    self._collect_external(
                        phases=phases,
                        tasks=external_research_tasks,
                        target_time_window=target_time_window,
                        as_of=as_of,
                    ),
                    return_exceptions=True,
                )
                hits, internal_failed = _raise_if_exception(internal_result)
                external = _raise_if_exception(external_result)
                return EvidenceCollectionOutcome(
                    internal_hits=hits,
                    external_search=external,
                    collection_failures=(
                        ["internal_search"] if internal_failed else []
                    ),
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def _collect_internal(
        self,
        *,
        phases: AnsweringPhases,
        queries: InternalSearchQueries,
    ) -> tuple[list[InternalArticleSearchHit], bool]:
        try:
            return await phases.internal_search.search_articles(queries), False
        except InternalSearchError:
            return [], True

    async def _collect_external(
        self,
        *,
        phases: AnsweringPhases,
        tasks: list[ExternalResearchTask],
        target_time_window: str | None,
        as_of: datetime,
    ) -> ExternalSearchOutcome:
        async with phases.external_runtime_factory.activate() as external:
            return await phases.external_search.search(
                tasks,
                target_time_window=target_time_window,
                as_of=as_of,
                external=external,
            )

    async def _report_progress(self, stage: AnswerProgressStage) -> None:
        if self._progress is None:
            return
        await self._progress.stage_changed(stage)


@contextmanager
def _answering_run_span(*, run_id: UUID) -> Iterator[None]:
    """正常な停止制御を error にせず、同じ例外を span 終了後に再送出する。"""
    stopped: AnswerGenerationStopped | None = None
    with logfire.span(_SPAN_NAME, run_id=str(run_id)):
        try:
            yield
        except AnswerGenerationStopped as exc:
            stopped = exc
    if stopped is not None:
        raise stopped


def _latest_assistant_answer(
    history: tuple[ThreadMessageSnapshot, ...],
) -> str:
    return next(
        (
            message.content
            for message in reversed(history)
            if message.role == "assistant"
        ),
        "",
    )


def _plan_target_time_window(plan: RetrievalPlan) -> str | None:
    match plan:
        case ExternalSearchPlan() | InternalAndExternalPlan():
            return plan.target_time_window
        case InternalRetrievalPlan():
            return None
    assert_never(plan)


def _raise_if_exception[ResultT](result: ResultT | BaseException) -> ResultT:
    if isinstance(result, BaseException):
        raise result
    return result
