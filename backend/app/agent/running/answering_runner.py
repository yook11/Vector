"""1回の回答処理を進行する AnsweringRunner。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import assert_never
from uuid import UUID

import logfire
from logfire import LogfireSpan

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.evidence import (
    normalize_answer_evidence,
)
from app.agent.answering.result_assembly import assemble_evidence_result
from app.agent.contract import (
    AnswerEventReporter,
    AnswerGenerationStopped,
    AnswerPlanSummary,
    AnswerProgressEvent,
    AnswerProgressReporter,
    AnswerProgressStage,
    AnswerQuestionResult,
    AnswerSource,
    EvidenceReviewSelectedEvent,
)
from app.agent.evidence_review import EvidenceCollectionOutcome, EvidenceReviewOutcome
from app.agent.evidence_review.run_review import review_collected_news
from app.agent.input_safety.contract import (
    INPUT_SAFETY_TEXT_CHAR_CAP,
    InputSafetyBlocked,
    InputSafetyChecker,
)
from app.agent.input_safety.history import previous_turn_from_history
from app.agent.planning.contract import (
    DirectAnswerPlan,
    PlanningRequest,
    SearchPlan,
    TargetTimeWindow,
)
from app.agent.research_checkpoint import (
    ResearchCheckpoint,
    build_research_checkpoint_or_none,
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
        input_safety_checker: InputSafetyChecker,
        context_preparer: QuestionContextPreparer,
        phases_factory: AnsweringPhasesFactory,
        progress: AnswerProgressReporter | None = None,
        events: AnswerEventReporter | None = None,
    ) -> None:
        self._input_safety_checker = input_safety_checker
        self._context_preparer = context_preparer
        self._phases_factory = phases_factory
        self._progress = progress
        self._events = events

    async def run(
        self,
        input: RunInput,
        *,
        run_context: RunContext,
        hooks: RunHooks | None = None,
    ) -> RunResult:
        with _answering_run_span(run_id=run_context.run_id) as run_span:
            await self._report_progress("safety_check")
            safety_check = await self._input_safety_checker.check(
                question=input.question[:INPUT_SAFETY_TEXT_CHAR_CAP],
                previous_turn=previous_turn_from_history(input.history),
                run_id=run_context.run_id,
            )
            if safety_check.is_blocked:
                assert safety_check.block_reason is not None  # noqa: S101
                raise InputSafetyBlocked(block_reason=safety_check.block_reason)

            await self._report_progress("context_resolution")
            question_context = await self._context_preparer.prepare(
                question=input.question,
                history=list(input.history),
                as_of=run_context.as_of,
                run_id=run_context.run_id,
            )
            answering_context = AnsweringRunContext(
                run_context=run_context,
                question_context=question_context,
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
                prior_research=input.prior_research,
            )
            answering_request = AnsweringRequest(
                context=answering_context.question_context,
                as_of=answering_context.run_context.as_of,
            )
            plan = await phases.planner.plan(planning_request)
            research_checkpoint: ResearchCheckpoint | None
            match plan:
                case DirectAnswerPlan():
                    answer = await self._answer_directly(
                        phases=phases,
                        request=answering_request,
                        previous_answer=answering_context.previous_answer,
                    )
                    research_checkpoint = None
                case SearchPlan():
                    await self._report_progress("evidence_collection")
                    date_filter, time_filter_failure = (
                        phases.collector.resolve_time_filter(
                            plan=plan,
                            as_of=answering_request.as_of,
                        )
                    )
                    # reviewerもLLM runtimeを使うため、scopeは収集と精査の両方を包む。
                    async with phases.external_runtime_factory.activate() as external:
                        collected_news = await phases.collector.collect(
                            plan=plan,
                            external=external,
                            date_filter=date_filter,
                            time_filter_failure=time_filter_failure,
                            as_of=answering_request.as_of,
                        )
                        if collected_news.has_candidates:
                            await self._report_progress("evidence_review")
                        reviewed = await review_collected_news(
                            collected_news=collected_news,
                            reviewer=phases.reviewer,
                            external=external,
                            as_of=answering_request.as_of,
                        )
                        if reviewed.review_outcome is not None:
                            await self._report_selected_evidence(
                                outcome=reviewed.review_outcome
                            )
                    research_checkpoint = build_research_checkpoint_or_none(
                        plan=plan,
                        collected_news=collected_news,
                        reviewed=reviewed,
                        as_of=answering_request.as_of,
                    )
                    answer = await self._answer_from_evidence(
                        phases=phases,
                        request=answering_request,
                        plan=plan,
                        reviewed_evidence=reviewed.outcome,
                        run_span=run_span,
                    )
                case _ as unreachable:
                    assert_never(unreachable)
            return RunResult(
                final_output=answer,
                context=answering_context,
                research_checkpoint=research_checkpoint,
            )

    async def _answer_directly(
        self,
        *,
        phases: AnsweringPhases,
        request: AnsweringRequest,
        previous_answer: str,
    ) -> AnswerQuestionResult:
        await self._report_progress("answering")
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

    async def _answer_from_evidence(
        self,
        *,
        phases: AnsweringPhases,
        request: AnsweringRequest,
        plan: SearchPlan,
        reviewed_evidence: EvidenceCollectionOutcome,
        run_span: LogfireSpan,
    ) -> AnswerQuestionResult:
        evidence = normalize_answer_evidence(reviewed_evidence)

        await self._report_progress("answering")
        answer_outcome = await phases.evidence_answerer.answer(
            request=request,
            evidence=evidence,
            target_time_window=_plan_target_time_window(plan),
            review_missing=tuple(reviewed_evidence.review.missing),
        )
        result = assemble_evidence_result(
            plan=plan,
            outcome=reviewed_evidence,
            evidence=evidence,
            answer_outcome=answer_outcome,
        )
        _record_evidence_span_attributes(
            run_span, outcome=reviewed_evidence, sources=result.sources
        )
        return result

    async def _report_selected_evidence(
        self,
        *,
        outcome: EvidenceReviewOutcome,
    ) -> None:
        """精査成功後、Run全体の採用件数を1本だけ発火する。"""
        await self._report_event(
            EvidenceReviewSelectedEvent(
                evidence_count=len(outcome.internal_evidence)
                + len(outcome.external_evidence),
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
