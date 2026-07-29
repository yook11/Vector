"""1回の回答処理を進行する AnsweringRunner。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never
from uuid import UUID

import logfire
from logfire import LogfireSpan

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
    EvidenceCollectionOutcome,
    ResearchTaskCandidates,
    ResearchTaskReport,
)
from app.agent.evidence_collection.contract import (
    TaskExternalCollectionStatus,
    TaskInternalCollectionStatus,
    TaskReviewStatus,
)
from app.agent.evidence_collection.evidence_review import InternalArticleEvidence
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
    ExternalSearchDateFilter,
    ExternalSearchEvidence,
    ExternalSearchOutcome,
    TimeFilterFailureReason,
)
from app.agent.evidence_collection.external_search.observability import (
    observe_time_filter_resolution,
)
from app.agent.evidence_collection.external_search.policy import (
    deduplicate_external_evidence_by_url,
    resolve_external_search_agent_count,
)
from app.agent.evidence_collection.external_search.time_filter import (
    ExternalSearchDateFilterResolutionError,
    resolve_external_search_date_filter,
)
from app.agent.input_safety.contract import (
    INPUT_SAFETY_TEXT_CHAR_CAP,
    InputSafetyBlocked,
    InputSafetyChecker,
    InputSafetyPreviousTurn,
)
from app.agent.planning.contract import (
    DirectAnswerPlan,
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
from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = ["AnsweringRunner"]

_SPAN_NAME = "agent_answering_run"


@dataclass(frozen=True, slots=True)
class _TaskResult:
    """1 taskのResearcher収集+Reviewer精査結果。合流前の中間値。"""

    internal_evidence: list[InternalArticleEvidence]
    external_evidence: list[ExternalSearchEvidence]
    report: ResearchTaskReport


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
            plan_summary=AnswerPlanSummary(plan_type="direct_answer"),
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
        content_requirements = tuple(
            requirement.description
            for requirement in request.context.content_requirements
        )
        outcome = await self._collect_evidence(
            phases=phases,
            plan=plan,
            content_requirements=content_requirements,
            as_of=request.as_of,
        )
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
            run_span, outcome=outcome, sources=result.sources
        )
        return result

    async def _collect_evidence(
        self,
        *,
        phases: AnsweringPhases,
        plan: SearchPlan,
        content_requirements: tuple[str, ...],
        as_of: datetime,
    ) -> EvidenceCollectionOutcome:
        tasks = plan.research_tasks
        time_filter_failure: TimeFilterFailureReason | None = None
        try:
            date_filter = resolve_external_search_date_filter(
                plan.target_time_window,
                as_of=as_of,
            )
        except ExternalSearchDateFilterResolutionError as exc:
            date_filter = None
            time_filter_failure = exc.reason
            observe_time_filter_resolution(
                result="failed",
                reason=exc.reason,
                task_count=len(tasks),
            )
        else:
            observe_time_filter_resolution(
                result="not_requested" if date_filter is None else "resolved",
                reason="none",
                task_count=len(tasks),
            )

        # reviewerがLLM runtimeを必要とするため、time filter失敗時も含め常にscopeを
        # activateする(外部query/HTTP検索だけをtask単位でskipする)。
        async with phases.external_runtime_factory.activate() as external:
            return await self._fan_out_tasks(
                phases=phases,
                plan=plan,
                tasks=tasks,
                external=external,
                date_filter=date_filter,
                time_filter_failure=time_filter_failure,
                content_requirements=content_requirements,
                as_of=as_of,
            )

    async def _fan_out_tasks(
        self,
        *,
        phases: AnsweringPhases,
        plan: SearchPlan,
        tasks: list[ResearchTask],
        external: ExternalResearchRuntime,
        date_filter: ExternalSearchDateFilter | None,
        time_filter_failure: TimeFilterFailureReason | None,
        content_requirements: tuple[str, ...],
        as_of: datetime,
    ) -> EvidenceCollectionOutcome:
        effective_agent_count = resolve_external_search_agent_count(
            task_count=len(tasks),
            requested_agent_count=self._requested_external_agent_count,
        )
        semaphore = asyncio.Semaphore(max(1, effective_agent_count))

        async def run_task(task_index: int, task: ResearchTask) -> _TaskResult:
            async with semaphore:
                return await self._collect_and_review(
                    phases=phases,
                    task_index=task_index,
                    task=task,
                    external=external,
                    date_filter=date_filter,
                    time_filter_failure=time_filter_failure,
                    target_time_window=plan.target_time_window,
                    content_requirements=content_requirements,
                    as_of=as_of,
                )

        results = await gather_cancel_on_error(
            *[run_task(task_index, task) for task_index, task in enumerate(tasks)]
        )

        internal_evidence: list[InternalArticleEvidence] = []
        external_evidence: list[ExternalSearchEvidence] = []
        reports: list[ResearchTaskReport] = []
        for result in results:
            internal_evidence.extend(result.internal_evidence)
            external_evidence.extend(result.external_evidence)
            reports.append(result.report)

        deduplicated_internal_evidence, internal_deduplicated_count = (
            _deduplicate_internal_evidence_by_curation_id(internal_evidence)
        )
        deduplicated_evidence, deduplicated_count = (
            deduplicate_external_evidence_by_url(external_evidence)
        )
        return EvidenceCollectionOutcome(
            internal_evidence=deduplicated_internal_evidence,
            internal_deduplicated_count=internal_deduplicated_count,
            external_search=ExternalSearchOutcome(
                evidence=deduplicated_evidence,
                deduplicated_evidence_count=deduplicated_count,
                requested_agent_count=self._requested_external_agent_count,
                effective_agent_count=effective_agent_count,
            ),
            task_reports=reports,
        )

    async def _collect_and_review(
        self,
        *,
        phases: AnsweringPhases,
        task_index: int,
        task: ResearchTask,
        external: ExternalResearchRuntime,
        date_filter: ExternalSearchDateFilter | None,
        time_filter_failure: TimeFilterFailureReason | None,
        target_time_window: TargetTimeWindow | None,
        content_requirements: tuple[str, ...],
        as_of: datetime,
    ) -> _TaskResult:
        # time filter失敗taskは外部収集自体を行わない(scopeは開いたまま)。
        collected = await phases.researcher.collect(
            task_index=task_index,
            task=task,
            external=None if time_filter_failure is not None else external,
            date_filter=None if time_filter_failure is not None else date_filter,
            target_time_window=target_time_window,
            as_of=as_of,
        )
        internal_evidence, external_evidence, report = await self._build_task_result(
            phases=phases,
            task_index=task_index,
            task=task,
            collected=collected,
            external=external,
            time_filter_failure=time_filter_failure,
            content_requirements=content_requirements,
            as_of=as_of,
        )
        return _TaskResult(
            internal_evidence=internal_evidence,
            external_evidence=external_evidence,
            report=report,
        )

    async def _build_task_result(
        self,
        *,
        phases: AnsweringPhases,
        task_index: int,
        task: ResearchTask,
        collected: ResearchTaskCandidates,
        external: ExternalResearchRuntime,
        time_filter_failure: TimeFilterFailureReason | None,
        content_requirements: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[
        list[InternalArticleEvidence], list[ExternalSearchEvidence], ResearchTaskReport
    ]:
        internal_collection: TaskInternalCollectionStatus = (
            "failed" if collected.internal_failed else "succeeded"
        )
        external_collection, generated_queries, provider_failed_query_count = (
            _external_collection_fields(
                collected=collected,
                time_filter_failure=time_filter_failure,
            )
        )
        internal_hits = collected.internal_hits
        external_candidates = collected.candidate_pool
        internal_candidate_count = len(internal_hits)
        external_candidate_count = len(external_candidates)
        collection = _CollectionReportFields(
            task_index=task_index,
            research_goal=task.research_goal,
            internal_collection=internal_collection,
            external_collection=external_collection,
            time_filter_failure_reason=time_filter_failure,
            generated_queries=generated_queries,
            provider_failed_query_count=provider_failed_query_count,
        )

        if internal_candidate_count == 0 and external_candidate_count == 0:
            return [], [], _build_task_report(collection, review="skipped_empty")

        outcome = await phases.reviewer.review(
            task_index=task_index,
            task=task,
            content_requirements=content_requirements,
            internal_hits=internal_hits,
            external_candidates=external_candidates,
            as_of=as_of,
            reviewer_runtime=external.reviewer_runtime,
        )

        if outcome.failure_reason is not None:
            report = _build_task_report(
                collection,
                review="failed",
                internal_candidate_count=internal_candidate_count,
                external_candidate_count=external_candidate_count,
                review_failure_reason=outcome.failure_reason,
            )
            return [], [], report

        await self._report_event(
            ExternalSearchEvidenceSelectedEvent(
                task_index=task_index,
                # 精査は内外統合のため、frontend表示が過少にならないよう合算する。
                evidence_count=len(outcome.internal_evidence)
                + len(outcome.external_evidence),
            )
        )
        report = _build_task_report(
            collection,
            review="succeeded",
            internal_candidate_count=internal_candidate_count,
            external_candidate_count=external_candidate_count,
            internal_evidence_count=len(outcome.internal_evidence),
            external_evidence_count=len(outcome.external_evidence),
            dropped_selection_count=outcome.dropped_selection_count,
            missing=outcome.missing,
        )
        return outcome.internal_evidence, outcome.external_evidence, report

    async def _report_event(self, event: AnswerProgressEvent) -> None:
        if self._events is None:
            return
        await self._events.event_occurred(event)

    async def _report_progress(self, stage: AnswerProgressStage) -> None:
        if self._progress is None:
            return
        await self._progress.stage_changed(stage)


def _external_collection_fields(
    *,
    collected: ResearchTaskCandidates,
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


@dataclass(frozen=True, slots=True)
class _CollectionReportFields:
    """review分岐(skipped_empty/failed/succeeded)に共通する収集側diagnostics。"""

    task_index: int
    research_goal: str
    internal_collection: TaskInternalCollectionStatus
    external_collection: TaskExternalCollectionStatus
    time_filter_failure_reason: TimeFilterFailureReason | None
    generated_queries: list[str]
    provider_failed_query_count: int


def _build_task_report(
    collection: _CollectionReportFields,
    *,
    review: TaskReviewStatus,
    internal_candidate_count: int = 0,
    external_candidate_count: int = 0,
    review_failure_reason: str | None = None,
    internal_evidence_count: int = 0,
    external_evidence_count: int = 0,
    dropped_selection_count: int = 0,
    missing: list[str] | None = None,
) -> ResearchTaskReport:
    """review分岐を1箇所でResearchTaskReportへ写像する(validatorの閉包と1対1)。"""
    return ResearchTaskReport(
        task_index=collection.task_index,
        research_goal=collection.research_goal,
        internal_collection=collection.internal_collection,
        external_collection=collection.external_collection,
        time_filter_failure_reason=collection.time_filter_failure_reason,
        generated_queries=collection.generated_queries,
        provider_failed_query_count=collection.provider_failed_query_count,
        internal_candidate_count=internal_candidate_count,
        external_candidate_count=external_candidate_count,
        review=review,
        review_failure_reason=review_failure_reason,
        internal_evidence_count=internal_evidence_count,
        external_evidence_count=external_evidence_count,
        dropped_selection_count=dropped_selection_count,
        missing=missing or [],
    )


def _deduplicate_internal_evidence_by_curation_id(
    evidence: list[InternalArticleEvidence],
) -> tuple[list[InternalArticleEvidence], int]:
    """内部記事識別子(curation_id)の先勝ちでrun単位の重複を除く。"""
    deduplicated: list[InternalArticleEvidence] = []
    seen_curation_ids: set[int] = set()
    dropped_count = 0
    for item in evidence:
        if item.curation_id in seen_curation_ids:
            dropped_count += 1
            continue
        deduplicated.append(item)
        seen_curation_ids.add(item.curation_id)
    return deduplicated, dropped_count


def _record_evidence_span_attributes(
    span: LogfireSpan,
    *,
    outcome: EvidenceCollectionOutcome,
    sources: list[AnswerSource],
) -> None:
    """内部・外部別の採用数と引用数、内部合流の統計を件数のみでspanへ焼く。"""
    external_evidence_count = (
        len(outcome.external_search.evidence)
        if outcome.external_search is not None
        else 0
    )
    span.set_attribute("internal_evidence_count", len(outcome.internal_evidence))
    span.set_attribute("external_evidence_count", external_evidence_count)
    span.set_attribute(
        "internal_cited_count",
        sum(1 for source in sources if source.kind == "internal_article"),
    )
    span.set_attribute(
        "external_cited_count",
        sum(1 for source in sources if source.kind == "external_url"),
    )
    span.set_attribute(
        "internal_deduplicated_count", outcome.internal_deduplicated_count
    )
    span.set_attribute(
        "internal_collection_failed_task_count",
        sum(
            1
            for report in outcome.task_reports
            if report.internal_collection == "failed"
        ),
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
