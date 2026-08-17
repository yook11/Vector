"""1回の回答処理を進行する AnsweringRunner。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import assert_never
from uuid import UUID

import logfire
from logfire import LogfireSpan

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.evidence import (
    build_answer_input_evidence,
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
from app.agent.evidence_collection import CollectedNews
from app.agent.evidence_collection.external_search import (
    EXTERNAL_SEARCH_AGENT_HARD_LIMIT,
    ExternalResearchRuntime,
)
from app.agent.evidence_review import (
    AnswerEvidence,
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
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
                        evidence_run = await self._review_evidence(
                            phases=phases,
                            collected_news=collected_news,
                            external=external,
                            as_of=answering_request.as_of,
                        )
                        _record_evidence_run_span_attributes(
                            run_span,
                            collected_news=collected_news,
                            evidence_run=evidence_run,
                        )
                    research_checkpoint = build_research_checkpoint_or_none(
                        plan=plan,
                        collected_news=collected_news,
                        evidence_run=evidence_run,
                        as_of=answering_request.as_of,
                    )
                    answer = await self._answer_from_evidence(
                        phases=phases,
                        request=answering_request,
                        plan=plan,
                        collected_news=collected_news,
                        evidence_run=evidence_run,
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

    async def _review_evidence(
        self,
        *,
        phases: AnsweringPhases,
        collected_news: CollectedNews,
        external: ExternalResearchRuntime,
        as_of: datetime,
    ) -> EvidenceRunResult:
        """ヒットゼロのRunは精査を開始せず、stageもselected eventにも反映されない。"""
        if not collected_news.has_hits:
            return EvidenceRunCompleted(
                answer_evidence=AnswerEvidence(),
                review_missing=(),
            )

        await self._report_progress("evidence_review")
        evidence_run = await phases.reviewer.review(
            tasks=collected_news.tasks,
            as_of=as_of,
            reviewer_runtime=external.reviewer_runtime,
        )
        if isinstance(evidence_run, EvidenceRunCompleted):
            await self._report_selected_evidence(evidence_run=evidence_run)
        return evidence_run

    async def _answer_from_evidence(
        self,
        *,
        phases: AnsweringPhases,
        request: AnsweringRequest,
        plan: SearchPlan,
        collected_news: CollectedNews,
        evidence_run: EvidenceRunResult,
        run_span: LogfireSpan,
    ) -> AnswerQuestionResult:
        if isinstance(evidence_run, EvidenceRunCompleted):
            answer_evidence = evidence_run.answer_evidence
            review_missing = evidence_run.review_missing
        else:
            answer_evidence = AnswerEvidence()
            review_missing = ()
        evidence = build_answer_input_evidence(answer_evidence)

        await self._report_progress("answering")
        answer_outcome = await phases.evidence_answerer.answer(
            request=request,
            evidence=evidence,
            target_time_window=_plan_target_time_window(plan),
            review_missing=review_missing,
        )
        result = assemble_evidence_result(
            plan=plan,
            collected_news=collected_news,
            evidence_run=evidence_run,
            evidence=evidence,
            answer_outcome=answer_outcome,
        )
        _record_citation_span_attributes(run_span, sources=result.sources)
        return result

    async def _report_selected_evidence(
        self,
        *,
        evidence_run: EvidenceRunCompleted,
    ) -> None:
        """精査成功後、Run全体の採用件数を1本だけ発火する。"""
        await self._report_event(
            EvidenceReviewSelectedEvent(
                evidence_count=evidence_run.answer_evidence.count,
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


def _record_evidence_run_span_attributes(
    span: LogfireSpan,
    *,
    collected_news: CollectedNews,
    evidence_run: EvidenceRunResult,
) -> None:
    """収集・精査のRun診断を安全な値に限ってspanへ焼く。"""
    answer_evidence = (
        evidence_run.answer_evidence
        if isinstance(evidence_run, EvidenceRunCompleted)
        else AnswerEvidence()
    )
    span.set_attribute(
        "internal_evidence_count",
        len(answer_evidence.internal_evidence),
    )
    span.set_attribute(
        "external_evidence_count",
        len(answer_evidence.external_evidence),
    )
    span.set_attribute(
        "internal_collection_failed_task_count",
        sum(
            1
            for task in collected_news.tasks
            if task.report.internal_collection == "failed"
        ),
    )
    if collected_news.requested_agent_count is not None:
        span.set_attribute(
            "requested_external_agent_count",
            collected_news.requested_agent_count,
        )
    span.set_attribute(
        "effective_external_agent_count",
        collected_news.effective_agent_count,
    )
    span.set_attribute(
        "external_agent_hard_limit",
        EXTERNAL_SEARCH_AGENT_HARD_LIMIT,
    )
    if isinstance(evidence_run, EvidenceRunFailed):
        span.set_attribute("review_failure_reason", evidence_run.failure_reason)


def _record_citation_span_attributes(
    span: LogfireSpan,
    *,
    sources: list[AnswerSource],
) -> None:
    """内部・外部別の引用数をrun spanへ焼く。"""
    span.set_attribute(
        "internal_cited_count",
        sum(1 for source in sources if source.kind == "internal_article"),
    )
    span.set_attribute(
        "external_cited_count",
        sum(1 for source in sources if source.kind == "external_url"),
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
