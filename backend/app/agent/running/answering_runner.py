"""1回の回答処理を進行する AnsweringRunner。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from typing import assert_never
from uuid import UUID

import logfire
from opentelemetry.trace import StatusCode
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.evidence import (
    normalize_answer_evidence,
)
from app.agent.answering.result_assembly import assemble_evidence_result
from app.agent.contract import (
    AnswerEventReporter,
    AnswerGenerationStopped,
    AnswerProgressEvent,
    AnswerProgressReporter,
    AnswerProgressStage,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    ExternalSearchCandidatesFetchedEvent,
    ExternalSearchEvidenceSelectedEvent,
    ExternalSearchQueriesGeneratedEvent,
)
from app.agent.evidence_collection.contract import EvidenceCollectionOutcome
from app.agent.evidence_collection.external_search.agent import (
    EXTERNAL_EVIDENCE_SELECTOR_AGENT,
    EXTERNAL_QUERY_AGENT,
)
from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
    EvidenceSelectionResult,
    ExternalEvidenceCandidateInput,
    ExternalEvidenceSelectionInput,
    ExternalQueryGenerationInput,
    ExternalResearchRuntime,
    ExternalSearchCandidate,
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    ExternalSearchProviderError,
    ExternalSearchTool,
    ExternalSearchToolInput,
    ResearchTaskReport,
    ResearchTaskStatus,
)
from app.agent.evidence_collection.external_search.policy import (
    EVIDENCE_SELECT_TIMEOUT_SECONDS,
    PROVIDER_SEARCH_TIMEOUT_SECONDS,
    QUERY_GENERATE_TIMEOUT_SECONDS,
    SELECTOR_TIMEOUT_REASON,
    build_candidate_pool,
    build_external_evidence,
    clean_generated_queries,
    deduplicate_external_evidence_by_url,
    finalize_selection_draft,
    resolve_external_search_agent_count,
    resolve_provider_failure_reason,
)
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
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
    AgentRuntime,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["AnsweringRunner"]

_SPAN_NAME = "agent_answering_run"
_EXTERNAL_PHASE_SPAN_NAME = "agent_phase"
_EXTERNAL_QUERY_PHASE = "external_query"
_EXTERNAL_SELECTOR_PHASE = "external_selector"


class AnsweringRunner:
    def __init__(
        self,
        *,
        context_preparer: QuestionContextPreparer,
        phases_factory: AnsweringPhasesFactory,
        progress: AnswerProgressReporter | None = None,
        events: AnswerEventReporter | None = None,
        requested_external_agent_count: int | None = None,
    ) -> None:
        self._context_preparer = context_preparer
        self._phases_factory = phases_factory
        self._progress = progress
        self._events = events
        self._requested_external_agent_count = requested_external_agent_count

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
            return await self._execute_external_pipeline(
                tasks=tasks,
                target_time_window=target_time_window,
                as_of=as_of,
                external=external,
            )

    async def _execute_external_pipeline(
        self,
        *,
        tasks: list[ExternalResearchTask],
        target_time_window: str | None,
        as_of: datetime,
        external: ExternalResearchRuntime,
    ) -> ExternalSearchOutcome:
        effective_agent_count = resolve_external_search_agent_count(
            task_count=len(tasks),
            requested_agent_count=self._requested_external_agent_count,
        )
        if not tasks:
            return ExternalSearchOutcome(
                tasks=tasks,
                requested_agent_count=self._requested_external_agent_count,
                effective_agent_count=effective_agent_count,
            )

        semaphore = asyncio.Semaphore(max(1, effective_agent_count))

        async def run_task(
            task_index: int,
            task: ExternalResearchTask,
        ) -> tuple[list[ExternalSearchEvidence], ResearchTaskReport]:
            async with semaphore:
                return await self._search_external_task(
                    task_index=task_index,
                    task=task,
                    target_time_window=target_time_window,
                    as_of=as_of,
                    external=external,
                )

        results = await _gather_cancel_on_error(
            *[run_task(task_index, task) for task_index, task in enumerate(tasks)]
        )
        evidence: list[ExternalSearchEvidence] = []
        reports: list[ResearchTaskReport] = []
        for task_evidence, report in results:
            evidence.extend(task_evidence)
            reports.append(report)
        deduplicated_evidence, deduplicated_count = (
            deduplicate_external_evidence_by_url(evidence)
        )
        return ExternalSearchOutcome(
            tasks=tasks,
            evidence=deduplicated_evidence,
            task_reports=reports,
            deduplicated_evidence_count=deduplicated_count,
            requested_agent_count=self._requested_external_agent_count,
            effective_agent_count=effective_agent_count,
        )

    async def _search_external_task(
        self,
        *,
        task_index: int,
        task: ExternalResearchTask,
        target_time_window: str | None,
        as_of: datetime,
        external: ExternalResearchRuntime,
    ) -> tuple[list[ExternalSearchEvidence], ResearchTaskReport]:
        query_input = ExternalQueryGenerationInput(
            task=task,
            as_of=as_of,
            target_time_window=target_time_window,
        )
        with _external_agent_phase(
            phase=_EXTERNAL_QUERY_PHASE,
            agent_name=EXTERNAL_QUERY_AGENT.name,
            task_index=task_index,
        ):
            try:
                query_draft = await asyncio.wait_for(
                    external.query_runtime.invoke(
                        EXTERNAL_QUERY_AGENT,
                        query_input,
                        attempt_number=1,
                    ),
                    timeout=QUERY_GENERATE_TIMEOUT_SECONDS,
                )
            except (AgentResponseInvalidError, AIProviderError, TimeoutError):
                return [], self._external_task_report(
                    task_index=task_index,
                    task=task,
                    status="query_generation_failed",
                )

        queries = clean_generated_queries(query_draft.queries)
        if not queries:
            return [], self._external_task_report(
                task_index=task_index,
                task=task,
                status="query_generation_failed",
            )
        await self._report_event(
            ExternalSearchQueriesGeneratedEvent(
                task_index=task_index,
                queries=queries,
            )
        )

        query_candidates: list[list[ExternalSearchCandidate]] = []
        provider_failed_query_count = 0
        provider_results = await _gather_cancel_on_error(
            *[
                self._search_external_query(query, search_tool=external.search_tool)
                for query in queries
            ]
        )
        for candidates, failed in provider_results:
            if failed:
                provider_failed_query_count += 1
                query_candidates.append([])
                continue
            query_candidates.append(candidates)

        if provider_failed_query_count == len(queries):
            return [], self._external_task_report(
                task_index=task_index,
                task=task,
                status="provider_failed",
                generated_queries=queries,
                provider_failed_query_count=provider_failed_query_count,
            )

        pool = build_candidate_pool(query_candidates)
        await self._report_event(
            ExternalSearchCandidatesFetchedEvent(
                task_index=task_index,
                candidate_count=len(pool),
            )
        )
        if not pool:
            return [], self._external_task_report(
                task_index=task_index,
                task=task,
                status="succeeded",
                generated_queries=queries,
                provider_failed_query_count=provider_failed_query_count,
                candidate_count=0,
            )

        (
            selection_result,
            selector_failure_reason,
        ) = await self._select_external_evidence(
            task=task,
            candidates=pool,
            as_of=as_of,
            task_index=task_index,
            selector_runtime=external.selector_runtime,
        )
        if selection_result is None:
            return [], self._external_task_report(
                task_index=task_index,
                task=task,
                status="selector_failed",
                generated_queries=queries,
                provider_failed_query_count=provider_failed_query_count,
                candidate_count=len(pool),
                selector_failure_reason=selector_failure_reason,
            )

        evidence, dropped_selection_count = build_external_evidence(
            task_index=task_index,
            pool=pool,
            selection_result=selection_result,
        )
        await self._report_event(
            ExternalSearchEvidenceSelectedEvent(
                task_index=task_index,
                evidence_count=len(evidence),
            )
        )
        return evidence, self._external_task_report(
            task_index=task_index,
            task=task,
            status="succeeded",
            generated_queries=queries,
            provider_failed_query_count=provider_failed_query_count,
            candidate_count=len(pool),
            evidence_count=len(evidence),
            dropped_selection_count=dropped_selection_count,
            missing=selection_result.missing,
        )

    async def _search_external_query(
        self,
        query: str,
        *,
        search_tool: ExternalSearchTool,
    ) -> tuple[list[ExternalSearchCandidate], bool]:
        try:
            candidates = await asyncio.wait_for(
                search_tool.invoke(
                    ExternalSearchToolInput(
                        query=query,
                        limit=EXTERNAL_SEARCH_CANDIDATES_PER_QUERY,
                    )
                ),
                timeout=PROVIDER_SEARCH_TIMEOUT_SECONDS,
            )
        except (ExternalSearchProviderError, TimeoutError):
            return [], True
        return candidates[:EXTERNAL_SEARCH_CANDIDATES_PER_QUERY], False

    async def _select_external_evidence(
        self,
        *,
        task: ExternalResearchTask,
        candidates: list[ExternalSearchCandidate],
        as_of: datetime,
        task_index: int,
        selector_runtime: AgentRuntime,
    ) -> tuple[EvidenceSelectionResult | None, str | None]:
        selector_input = ExternalEvidenceSelectionInput(
            task=task,
            candidates=tuple(
                ExternalEvidenceCandidateInput(
                    index=index,
                    title=candidate.title,
                    source_name=candidate.source_name,
                    published_at=candidate.published_at,
                    snippet=candidate.snippet,
                )
                for index, candidate in enumerate(candidates)
            ),
            as_of=as_of,
        )
        selector_failure_reason: str | None = None
        with _external_agent_phase(
            phase=_EXTERNAL_SELECTOR_PHASE,
            agent_name=EXTERNAL_EVIDENCE_SELECTOR_AGENT.name,
            task_index=task_index,
        ):
            for attempt_number in range(1, 3):
                try:
                    draft = await asyncio.wait_for(
                        selector_runtime.invoke(
                            EXTERNAL_EVIDENCE_SELECTOR_AGENT,
                            selector_input,
                            attempt_number=attempt_number,
                        ),
                        timeout=EVIDENCE_SELECT_TIMEOUT_SECONDS,
                    )
                except AgentResponseInvalidError as exc:
                    selector_failure_reason = exc.defect.value
                    continue
                except AIProviderError as exc:
                    selector_failure_reason = _provider_failure_reason(exc)
                    continue
                except TimeoutError:
                    selector_failure_reason = SELECTOR_TIMEOUT_REASON
                    continue

                try:
                    selection_result = finalize_selection_draft(draft)
                except ValidationError:
                    selector_failure_reason = (
                        AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH.value
                    )
                    continue
                return selection_result, None
        return None, selector_failure_reason

    @staticmethod
    def _external_task_report(
        *,
        task_index: int,
        task: ExternalResearchTask,
        status: ResearchTaskStatus,
        generated_queries: list[str] | None = None,
        provider_failed_query_count: int = 0,
        candidate_count: int = 0,
        evidence_count: int = 0,
        dropped_selection_count: int = 0,
        selector_failure_reason: str | None = None,
        missing: list[str] | None = None,
    ) -> ResearchTaskReport:
        return ResearchTaskReport.from_raw(
            task_index=task_index,
            collection_goal=task.collection_goal,
            generated_queries=generated_queries,
            status=status,
            provider_failed_query_count=provider_failed_query_count,
            candidate_count=candidate_count,
            evidence_count=evidence_count,
            dropped_selection_count=dropped_selection_count,
            selector_failure_reason=selector_failure_reason,
            missing=missing,
        )

    async def _report_event(self, event: AnswerProgressEvent) -> None:
        if self._events is None:
            return
        await self._events.event_occurred(event)

    async def _report_progress(self, stage: AnswerProgressStage) -> None:
        if self._progress is None:
            return
        await self._progress.stage_changed(stage)


async def _gather_cancel_on_error[ResultT](
    *awaitables: Awaitable[ResultT],
) -> list[ResultT]:
    """未分類例外時に兄弟処理をcancelして合流してから元の例外を返す。"""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _provider_failure_reason(exc: AIProviderError) -> str:
    reason = getattr(exc, "reason", None)
    reason_value = reason.value if isinstance(reason, StrEnum) else None
    code = getattr(exc, "CODE", None)
    return resolve_provider_failure_reason(
        reason=reason_value,
        code=code if isinstance(code, str) else None,
    )


@contextmanager
def _external_agent_phase(
    *,
    phase: str,
    agent_name: str,
    task_index: int,
) -> Iterator[None]:
    """External task単位のAgent policy spanを作る。"""
    if task_index < 0:
        raise ValueError("task_index must be non-negative")
    with logfire.span(
        _EXTERNAL_PHASE_SPAN_NAME,
        phase=phase,
        agent_name=agent_name,
        task_index=task_index,
    ) as span:
        try:
            yield
        except BaseException:
            span.set_status(StatusCode.ERROR, "unclassified agent phase error")
            raise


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
