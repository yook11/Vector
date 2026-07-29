"""1回の回答処理を進行する AnsweringRunner。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import assert_never
from uuid import UUID

import logfire
from logfire import LogfireSpan
from pydantic import ValidationError

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.evidence import (
    normalize_answer_evidence,
)
from app.agent.answering.result_assembly import assemble_evidence_result
from app.agent.concurrency import gather_cancel_on_error
from app.agent.contract import (
    AnswerEventReporter,
    AnswerGenerationStopped,
    AnswerPlanSummary,
    AnswerProgressEvent,
    AnswerProgressReporter,
    AnswerProgressStage,
    AnswerQuestionResult,
    AnswerSource,
    ExternalSearchEvidenceSelectedEvent,
)
from app.agent.evidence_collection import (
    ExternalCollectionStatus,
    ResearchTaskCandidates,
)
from app.agent.evidence_collection.contract import EvidenceCollectionOutcome
from app.agent.evidence_collection.external_search.agent import (
    EXTERNAL_EVIDENCE_SELECTOR_AGENT,
)
from app.agent.evidence_collection.external_search.contract import (
    EvidenceSelectionResult,
    ExternalEvidenceCandidateInput,
    ExternalEvidenceSelectionInput,
    ExternalResearchRuntime,
    ExternalSearchCandidate,
    ExternalSearchDateFilter,
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    ResearchTaskReport,
    ResearchTaskStatus,
    TimeFilterFailureReason,
)
from app.agent.evidence_collection.external_search.observability import (
    observe_time_filter_resolution,
)
from app.agent.evidence_collection.external_search.policy import (
    EVIDENCE_SELECT_TIMEOUT_SECONDS,
    SELECTOR_TIMEOUT_REASON,
    build_external_evidence,
    deduplicate_external_evidence_by_url,
    finalize_selection_draft,
    resolve_external_search_agent_count,
    resolve_provider_failure_reason,
)
from app.agent.evidence_collection.external_search.time_filter import (
    ExternalSearchDateFilterResolutionError,
    resolve_external_search_date_filter,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)
from app.agent.input_safety.contract import (
    INPUT_SAFETY_TEXT_CHAR_CAP,
    InputSafetyBlocked,
    InputSafetyChecker,
    InputSafetyPreviousTurn,
)
from app.agent.phase_span import agent_phase
from app.agent.planning.contract import (
    DirectAnswerPlan,
    ExternalResearchTask,
    PlanningRequest,
    ResearchTask,
    SearchPlan,
    TargetTimeWindow,
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
_EXTERNAL_SELECTOR_PHASE = "external_selector"


@dataclass(frozen=True, slots=True)
class _TaskResult:
    """1 taskのResearcher収集+選別結果。合流前の中間値。"""

    internal_hits: list[InternalArticleSearchHit]
    internal_failed: bool
    evidence: list[ExternalSearchEvidence]
    report: ResearchTaskReport


@dataclass(frozen=True, slots=True)
class _ExternalCollectionAvailable:
    """plan単位のtarget_time_window解決に成功し、外部runtimeが使える状態。"""

    runtime: ExternalResearchRuntime
    date_filter: ExternalSearchDateFilter | None


@dataclass(frozen=True, slots=True)
class _ExternalCollectionUnavailable:
    """plan単位のtarget_time_window解決に失敗し、外部収集を行えない状態。"""

    reason: TimeFilterFailureReason


type _ExternalCollectionPlan = (
    _ExternalCollectionAvailable | _ExternalCollectionUnavailable
)


@dataclass(frozen=True, slots=True)
class _EvidenceCollectionResult:
    """evidence収集の結果と、span報告用の内部合流統計。"""

    outcome: EvidenceCollectionOutcome
    internal_deduplicated_count: int
    internal_collection_failed_task_count: int


class AnsweringRunner:
    def __init__(
        self,
        *,
        input_safety_checker: InputSafetyChecker,
        context_preparer: QuestionContextPreparer,
        phases_factory: AnsweringPhasesFactory,
        progress: AnswerProgressReporter | None = None,
        events: AnswerEventReporter | None = None,
        requested_external_agent_count: int | None = None,
    ) -> None:
        self._input_safety_checker = input_safety_checker
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
        with _answering_run_span(run_id=run_context.run_id) as run_span:
            safety_check = await self._input_safety_checker.check(
                question=input.question[:INPUT_SAFETY_TEXT_CHAR_CAP],
                previous_turn=_previous_turn(input.history),
                run_id=run_context.run_id,
            )
            if safety_check.is_blocked:
                assert safety_check.block_reason is not None  # noqa: S101
                raise InputSafetyBlocked(block_reason=safety_check.block_reason)

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
                case DirectAnswerPlan():
                    final_output = await self._answer_directly(
                        phases=phases,
                        request=answering_request,
                        previous_answer=answering_context.previous_answer,
                    )
                case SearchPlan():
                    final_output = await self._answer_with_evidence(
                        phases=phases,
                        request=answering_request,
                        plan=plan,
                        run_span=run_span,
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
            plan_summary=AnswerPlanSummary(
                plan_type="direct_answer",
                collection_failures=[],
            ),
        )

    async def _answer_with_evidence(
        self,
        *,
        phases: AnsweringPhases,
        request: AnsweringRequest,
        plan: SearchPlan,
        run_span: LogfireSpan,
    ) -> AnswerQuestionResult:
        await self._report_progress("retrieving")
        collection_result = await self._collect_evidence(
            phases=phases,
            plan=plan,
            as_of=request.as_of,
        )
        outcome = collection_result.outcome
        evidence = normalize_answer_evidence(outcome)

        await self._report_progress("synthesizing")
        draft = await phases.evidence_answerer.answer(
            request=request,
            evidence=evidence,
            target_time_window=_plan_target_time_window(plan),
        )
        result = assemble_evidence_result(
            context=request.context,
            plan=plan,
            outcome=outcome,
            evidence=evidence,
            draft=draft,
        )
        _record_evidence_span_attributes(
            run_span,
            outcome=outcome,
            sources=result.sources,
            internal_deduplicated_count=collection_result.internal_deduplicated_count,
            internal_collection_failed_task_count=(
                collection_result.internal_collection_failed_task_count
            ),
        )
        return result

    async def _collect_evidence(
        self,
        *,
        phases: AnsweringPhases,
        plan: SearchPlan,
        as_of: datetime,
    ) -> _EvidenceCollectionResult:
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
            return await self._fan_out_tasks(
                phases=phases,
                plan=plan,
                tasks=tasks,
                external_collection=_ExternalCollectionUnavailable(reason=exc.reason),
                as_of=as_of,
            )

        observe_time_filter_resolution(
            result="not_requested" if date_filter is None else "resolved",
            reason="none",
            task_count=len(tasks),
        )
        async with phases.external_runtime_factory.activate() as external:
            return await self._fan_out_tasks(
                phases=phases,
                plan=plan,
                tasks=tasks,
                external_collection=_ExternalCollectionAvailable(
                    runtime=external,
                    date_filter=date_filter,
                ),
                as_of=as_of,
            )

    async def _fan_out_tasks(
        self,
        *,
        phases: AnsweringPhases,
        plan: SearchPlan,
        tasks: list[ResearchTask],
        external_collection: _ExternalCollectionPlan,
        as_of: datetime,
    ) -> _EvidenceCollectionResult:
        effective_agent_count = resolve_external_search_agent_count(
            task_count=len(tasks),
            requested_agent_count=self._requested_external_agent_count,
        )
        semaphore = asyncio.Semaphore(max(1, effective_agent_count))

        async def run_task(task_index: int, task: ResearchTask) -> _TaskResult:
            async with semaphore:
                return await self._collect_and_select(
                    phases=phases,
                    task_index=task_index,
                    task=task,
                    external_collection=external_collection,
                    target_time_window=plan.target_time_window,
                    as_of=as_of,
                )

        results = await gather_cancel_on_error(
            *[run_task(task_index, task) for task_index, task in enumerate(tasks)]
        )

        internal_hits: list[InternalArticleSearchHit] = []
        external_evidence: list[ExternalSearchEvidence] = []
        reports: list[ResearchTaskReport] = []
        internal_collection_failed_task_count = 0
        for result in results:
            internal_hits.extend(result.internal_hits)
            external_evidence.extend(result.evidence)
            reports.append(result.report)
            if result.internal_failed:
                internal_collection_failed_task_count += 1

        deduplicated_internal_hits, internal_deduplicated_count = (
            _deduplicate_internal_hits_by_curation_id(internal_hits)
        )
        deduplicated_evidence, deduplicated_count = (
            deduplicate_external_evidence_by_url(external_evidence)
        )
        outcome = ExternalSearchOutcome(
            tasks=[
                ExternalResearchTask(research_goal=task.research_goal) for task in tasks
            ],
            evidence=deduplicated_evidence,
            task_reports=reports,
            deduplicated_evidence_count=deduplicated_count,
            requested_agent_count=self._requested_external_agent_count,
            effective_agent_count=effective_agent_count,
        )
        return _EvidenceCollectionResult(
            outcome=EvidenceCollectionOutcome(
                internal_hits=deduplicated_internal_hits,
                external_search=outcome,
                collection_failures=(
                    ["internal_search"]
                    if internal_collection_failed_task_count == len(results)
                    else []
                ),
            ),
            internal_deduplicated_count=internal_deduplicated_count,
            internal_collection_failed_task_count=(
                internal_collection_failed_task_count
            ),
        )

    async def _collect_and_select(
        self,
        *,
        phases: AnsweringPhases,
        task_index: int,
        task: ResearchTask,
        external_collection: _ExternalCollectionPlan,
        target_time_window: TargetTimeWindow | None,
        as_of: datetime,
    ) -> _TaskResult:
        external_runtime, external_date_filter = _resolve_external_arguments(
            external_collection
        )
        collected = await phases.researcher.collect(
            task_index=task_index,
            task=task,
            external=external_runtime,
            date_filter=external_date_filter,
            target_time_window=target_time_window,
            as_of=as_of,
        )
        evidence, report = await self._build_task_result(
            task_index=task_index,
            task=task,
            collected=collected,
            external_collection=external_collection,
            as_of=as_of,
        )
        return _TaskResult(
            internal_hits=collected.internal_hits,
            internal_failed=collected.internal_failed,
            evidence=evidence,
            report=report,
        )

    async def _build_task_result(
        self,
        *,
        task_index: int,
        task: ResearchTask,
        collected: ResearchTaskCandidates,
        external_collection: _ExternalCollectionPlan,
        as_of: datetime,
    ) -> tuple[list[ExternalSearchEvidence], ResearchTaskReport]:
        external_task = ExternalResearchTask(research_goal=task.research_goal)
        match external_collection:
            case _ExternalCollectionUnavailable(reason=reason):
                return [], self._external_task_report(
                    task_index=task_index,
                    task=external_task,
                    status="time_filter_failed",
                    time_filter_failure_reason=reason,
                )
            case _ExternalCollectionAvailable(runtime=runtime):
                # 外部runtimeが利用可能な経路では、Researcherは必ずstatusを返す。
                external_status = collected.external_status
                assert external_status is not None  # noqa: S101
                return await self._build_status_task_result(
                    task_index=task_index,
                    external_task=external_task,
                    collected=collected,
                    external_status=external_status,
                    selector_runtime=runtime.selector_runtime,
                    as_of=as_of,
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def _build_status_task_result(
        self,
        *,
        task_index: int,
        external_task: ExternalResearchTask,
        collected: ResearchTaskCandidates,
        external_status: ExternalCollectionStatus,
        selector_runtime: AgentRuntime,
        as_of: datetime,
    ) -> tuple[list[ExternalSearchEvidence], ResearchTaskReport]:
        match external_status:
            case "query_generation_failed":
                return [], self._external_task_report(
                    task_index=task_index,
                    task=external_task,
                    status="query_generation_failed",
                )
            case "provider_failed":
                return [], self._external_task_report(
                    task_index=task_index,
                    task=external_task,
                    status="provider_failed",
                    generated_queries=collected.generated_queries,
                    provider_failed_query_count=collected.provider_failed_query_count,
                )
            case "succeeded":
                return await self._build_succeeded_task_result(
                    task_index=task_index,
                    external_task=external_task,
                    collected=collected,
                    selector_runtime=selector_runtime,
                    as_of=as_of,
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def _build_succeeded_task_result(
        self,
        *,
        task_index: int,
        external_task: ExternalResearchTask,
        collected: ResearchTaskCandidates,
        selector_runtime: AgentRuntime,
        as_of: datetime,
    ) -> tuple[list[ExternalSearchEvidence], ResearchTaskReport]:
        pool = collected.candidate_pool
        if not pool:
            return [], self._external_task_report(
                task_index=task_index,
                task=external_task,
                status="succeeded",
                generated_queries=collected.generated_queries,
                provider_failed_query_count=collected.provider_failed_query_count,
                candidate_count=0,
            )

        (
            selection_result,
            selector_failure_reason,
        ) = await self._select_external_evidence(
            task=external_task,
            candidates=pool,
            as_of=as_of,
            task_index=task_index,
            selector_runtime=selector_runtime,
        )
        if selection_result is None:
            return [], self._external_task_report(
                task_index=task_index,
                task=external_task,
                status="selector_failed",
                generated_queries=collected.generated_queries,
                provider_failed_query_count=collected.provider_failed_query_count,
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
            task=external_task,
            status="succeeded",
            generated_queries=collected.generated_queries,
            provider_failed_query_count=collected.provider_failed_query_count,
            candidate_count=len(pool),
            evidence_count=len(evidence),
            dropped_selection_count=dropped_selection_count,
            missing=selection_result.missing,
        )

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
        with agent_phase(
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
        time_filter_failure_reason: TimeFilterFailureReason | None = None,
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
            research_goal=task.research_goal,
            generated_queries=generated_queries,
            status=status,
            time_filter_failure_reason=time_filter_failure_reason,
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


def _resolve_external_arguments(
    external_collection: _ExternalCollectionPlan,
) -> tuple[ExternalResearchRuntime | None, ExternalSearchDateFilter | None]:
    """Researcher.collect()へ渡す2引数を、外部収集可否の型から一箇所で導出する。"""
    match external_collection:
        case _ExternalCollectionAvailable(runtime=runtime, date_filter=date_filter):
            return runtime, date_filter
        case _ExternalCollectionUnavailable():
            return None, None
        case _ as unreachable:
            assert_never(unreachable)


def _deduplicate_internal_hits_by_curation_id(
    hits: list[InternalArticleSearchHit],
) -> tuple[list[InternalArticleSearchHit], int]:
    """内部記事識別子(curation_id)の先勝ちでrun単位の重複を除く。"""
    deduplicated: list[InternalArticleSearchHit] = []
    seen_curation_ids: set[int] = set()
    dropped_count = 0
    for hit in hits:
        curation_id = hit.article.curation_id
        if curation_id in seen_curation_ids:
            dropped_count += 1
            continue
        deduplicated.append(hit)
        seen_curation_ids.add(curation_id)
    return deduplicated, dropped_count


def _record_evidence_span_attributes(
    span: LogfireSpan,
    *,
    outcome: EvidenceCollectionOutcome,
    sources: list[AnswerSource],
    internal_deduplicated_count: int,
    internal_collection_failed_task_count: int,
) -> None:
    """内部・外部別の採用数と引用数、内部合流の統計を件数のみでspanへ焼く。"""
    external_evidence_count = (
        len(outcome.external_search.evidence)
        if outcome.external_search is not None
        else 0
    )
    span.set_attribute("internal_evidence_count", len(outcome.internal_hits))
    span.set_attribute("external_evidence_count", external_evidence_count)
    span.set_attribute(
        "internal_cited_count",
        sum(1 for source in sources if source.kind == "internal_article"),
    )
    span.set_attribute(
        "external_cited_count",
        sum(1 for source in sources if source.kind == "external_url"),
    )
    span.set_attribute("internal_deduplicated_count", internal_deduplicated_count)
    span.set_attribute(
        "internal_collection_failed_task_count",
        internal_collection_failed_task_count,
    )


def _provider_failure_reason(exc: AIProviderError) -> str:
    reason = getattr(exc, "reason", None)
    reason_value = reason.value if isinstance(reason, StrEnum) else None
    code = getattr(exc, "CODE", None)
    return resolve_provider_failure_reason(
        reason=reason_value,
        code=code if isinstance(code, str) else None,
    )


@contextmanager
def _answering_run_span(*, run_id: UUID) -> Iterator[LogfireSpan]:
    """正常な停止制御を error にせず、同じ例外を span 終了後に再送出する。"""
    stopped: AnswerGenerationStopped | InputSafetyBlocked | None = None
    with logfire.span(_SPAN_NAME, run_id=str(run_id)) as span:
        try:
            yield span
        except (AnswerGenerationStopped, InputSafetyBlocked) as exc:
            stopped = exc
    if stopped is not None:
        raise stopped


def _previous_turn(
    history: tuple[ThreadMessageSnapshot, ...],
) -> InputSafetyPreviousTurn | None:
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if message.role != "user":
            continue
        assistant_answer: str | None = None
        if index + 1 < len(history):
            next_message = history[index + 1]
            if next_message.role == "assistant" and next_message.content:
                assistant_answer = next_message.content[:INPUT_SAFETY_TEXT_CHAR_CAP]
        return InputSafetyPreviousTurn(
            user_question=message.content[:INPUT_SAFETY_TEXT_CHAR_CAP],
            assistant_answer=assistant_answer,
        )
    return None


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


def _plan_target_time_window(plan: SearchPlan) -> TargetTimeWindow | None:
    return plan.target_time_window
