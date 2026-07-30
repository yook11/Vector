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
    EvidenceReviewReport,
    ResearchTaskCandidates,
    ResearchTaskReport,
)
from app.agent.evidence_collection.contract import (
    TaskExternalCollectionStatus,
    TaskInternalCollectionStatus,
)
from app.agent.evidence_collection.evidence_review import (
    EvidenceReviewOutcome,
    InternalArticleEvidence,
    ReviewTaskCandidates,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
    ExternalSearchCandidate,
    ExternalSearchDateFilter,
    ExternalSearchOutcome,
    TimeFilterFailureReason,
)
from app.agent.evidence_collection.external_search.observability import (
    observe_time_filter_resolution,
)
from app.agent.evidence_collection.external_search.policy import (
    resolve_external_search_agent_count,
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
class _TaskCollection:
    """1 taskのResearcher収集結果と収集onlyのreport。精査はRun単位で別途行う。"""

    task_index: int
    research_goal: str
    internal_hits: list[InternalArticleSearchHit]
    external_candidates: list[ExternalSearchCandidate]
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
            return await self._collect_and_review_all_tasks(
                phases=phases,
                plan=plan,
                tasks=tasks,
                external=external,
                date_filter=date_filter,
                time_filter_failure=time_filter_failure,
                content_requirements=content_requirements,
                as_of=as_of,
            )

    async def _collect_and_review_all_tasks(
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

        async def run_task(task_index: int, task: ResearchTask) -> _TaskCollection:
            async with semaphore:
                return await self._collect_task(
                    phases=phases,
                    task_index=task_index,
                    task=task,
                    external=external,
                    date_filter=date_filter,
                    time_filter_failure=time_filter_failure,
                    target_time_window=plan.target_time_window,
                    as_of=as_of,
                )

        collected_tasks = await gather_cancel_on_error(
            *[run_task(task_index, task) for task_index, task in enumerate(tasks)]
        )

        task_reports = [collected.report for collected in collected_tasks]
        review_candidates = [
            ReviewTaskCandidates(
                task_index=collected.task_index,
                research_goal=collected.research_goal,
                internal_hits=collected.internal_hits,
                external_candidates=collected.external_candidates,
            )
            for collected in collected_tasks
        ]

        # 全taskの収集が完了してから、Run全体を1回の入力として精査する
        # (収集は並列を維持したまま、精査だけがRun単位1回になる)。
        if not any(
            candidates.internal_hits or candidates.external_candidates
            for candidates in review_candidates
        ):
            return self._closed_evidence_outcome(
                review=EvidenceReviewReport(review="skipped_empty"),
                task_reports=task_reports,
                effective_agent_count=effective_agent_count,
            )

        outcome = await phases.reviewer.review(
            tasks=review_candidates,
            content_requirements=content_requirements,
            as_of=as_of,
            reviewer_runtime=external.reviewer_runtime,
        )

        if outcome.failure_reason is not None:
            return self._closed_evidence_outcome(
                review=EvidenceReviewReport(
                    review="failed",
                    review_failure_reason=outcome.failure_reason,
                ),
                task_reports=task_reports,
                effective_agent_count=effective_agent_count,
            )

        await self._report_selected_evidence_events(
            outcome=outcome, review_candidates=review_candidates
        )

        deduplicated_internal_evidence, internal_deduplicated_count = (
            _deduplicate_internal_evidence_by_curation_id(outcome.internal_evidence)
        )
        return EvidenceCollectionOutcome(
            internal_evidence=deduplicated_internal_evidence,
            internal_deduplicated_count=internal_deduplicated_count,
            external_search=ExternalSearchOutcome(
                evidence=outcome.external_evidence,
                requested_agent_count=self._requested_external_agent_count,
                effective_agent_count=effective_agent_count,
            ),
            task_reports=task_reports,
            review=EvidenceReviewReport(
                review="succeeded",
                internal_evidence_count=len(outcome.internal_evidence),
                external_evidence_count=len(outcome.external_evidence),
                dropped_selection_count=outcome.dropped_selection_count,
                missing=outcome.missing,
            ),
        )

    def _closed_evidence_outcome(
        self,
        *,
        review: EvidenceReviewReport,
        task_reports: list[ResearchTaskReport],
        effective_agent_count: int,
    ) -> EvidenceCollectionOutcome:
        """精査を呼ばなかった/失敗したRunを根拠ゼロで閉じる。"""
        return EvidenceCollectionOutcome(
            internal_evidence=[],
            internal_deduplicated_count=0,
            external_search=ExternalSearchOutcome(
                evidence=[],
                requested_agent_count=self._requested_external_agent_count,
                effective_agent_count=effective_agent_count,
            ),
            task_reports=task_reports,
            review=review,
        )

    async def _collect_task(
        self,
        *,
        phases: AnsweringPhases,
        task_index: int,
        task: ResearchTask,
        external: ExternalResearchRuntime,
        date_filter: ExternalSearchDateFilter | None,
        time_filter_failure: TimeFilterFailureReason | None,
        target_time_window: TargetTimeWindow | None,
        as_of: datetime,
    ) -> _TaskCollection:
        # time filter失敗taskは外部収集自体を行わない(scopeは開いたまま)。
        collected = await phases.researcher.collect(
            task_index=task_index,
            task=task,
            external=None if time_filter_failure is not None else external,
            date_filter=None if time_filter_failure is not None else date_filter,
            target_time_window=target_time_window,
            as_of=as_of,
        )
        internal_collection: TaskInternalCollectionStatus = (
            "failed" if collected.internal_failed else "succeeded"
        )
        external_collection, generated_queries, provider_failed_query_count = (
            _external_collection_fields(
                collected=collected,
                time_filter_failure=time_filter_failure,
            )
        )
        report = ResearchTaskReport(
            task_index=task_index,
            research_goal=task.research_goal,
            internal_collection=internal_collection,
            external_collection=external_collection,
            time_filter_failure_reason=time_filter_failure,
            generated_queries=generated_queries,
            provider_failed_query_count=provider_failed_query_count,
            internal_candidate_count=len(collected.internal_hits),
            external_candidate_count=len(collected.candidate_pool),
        )
        return _TaskCollection(
            task_index=task_index,
            research_goal=task.research_goal,
            internal_hits=collected.internal_hits,
            external_candidates=collected.candidate_pool,
            report=report,
        )

    async def _report_selected_evidence_events(
        self,
        *,
        outcome: EvidenceReviewOutcome,
        review_candidates: list[ReviewTaskCandidates],
    ) -> None:
        """精査成功後、候補があったtaskについてtask_index昇順で1回ずつ発火する。"""
        counts = {
            candidates.task_index: 0
            for candidates in review_candidates
            if candidates.internal_hits or candidates.external_candidates
        }
        for item in outcome.internal_evidence:
            counts[item.task_index] += 1
        for item in outcome.external_evidence:
            counts[item.task_index] += 1
        for task_index in sorted(counts):
            await self._report_event(
                ExternalSearchEvidenceSelectedEvent(
                    task_index=task_index,
                    evidence_count=counts[task_index],
                )
            )

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
