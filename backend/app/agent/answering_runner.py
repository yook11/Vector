"""1回の回答処理を進行する AnsweringRunner と、その工程・入力・出力。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, assert_never
from uuid import UUID

import logfire
from logfire import LogfireSpan

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.contract import (
    DirectAnswerDraft,
    DirectAnswerer,
    DirectAnswerInput,
)
from app.agent.answering.direct_answer.failure import DirectAnswerError
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerer,
    EvidenceAnswerInput,
)
from app.agent.answering.evidence_answer.evidence import (
    build_answer_input_evidence,
)
from app.agent.answering.evidence_answer.failure import EvidenceAnswerError
from app.agent.answering.result_assembly import assemble_evidence_result
from app.agent.contract import (
    AnswerEventReporter,
    AnswerPlanSummary,
    AnswerProgressEvent,
    AnswerQuestionResult,
    AnswerSource,
    EvidenceReviewSelectedEvent,
    StageGate,
)
from app.agent.evidence_collection import CollectedNews, EvidenceCollector
from app.agent.evidence_review import (
    AnswerEvidence,
    EvidenceReviewer,
    EvidenceRunCompleted,
    EvidenceRunFailed,
    EvidenceRunResult,
)
from app.agent.planning.contract import (
    DirectAnswerPlan,
    PlanningInput,
    QuestionPlanner,
    SearchPlan,
)
from app.agent.planning.failure import PlanningError
from app.agent.research_handoff.handoff import ResearchHandoff
from app.agent.research_handoff.handoff_input import ResearchHandoffInput
from app.agent.research_handoff.service import ResearchHandoffOrganizer
from app.agent.running.state import (
    AttemptSuperseded,
    CancelledByUser,
    CanContinue,
    CannotContinue,
    DeadlineExceeded,
    RunNotFound,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.ai_providers.errors import AIProviderError
from app.shared.errors import ApplicationError

__all__ = [
    "AnsweringPhases",
    "AnsweringPhasesFactory",
    "AnsweringRunError",
    "AnsweringRunner",
    "RunIdentity",
    "RunInput",
    "RunResult",
]

_SPAN_NAME = "agent_answering_run"
# 回答はストリームし終えているため、整理をこれ以上待たずRunを閉じる。
HANDOFF_ORGANIZE_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class AnsweringPhases:
    planner: QuestionPlanner
    collector: EvidenceCollector
    direct_answerer: DirectAnswerer
    evidence_answerer: EvidenceAnswerer
    reviewer: EvidenceReviewer
    organizer: ResearchHandoffOrganizer


class AnsweringPhasesFactory(Protocol):
    def __call__(self) -> AnsweringPhases: ...


@dataclass(frozen=True, slots=True)
class RunInput:
    question: str
    history: tuple[ThreadMessageSnapshot, ...]
    # 同threadの調査の申し送り。読出し・検証失敗時はNone。
    research_handoff: ResearchHandoff | None = None


@dataclass(frozen=True, slots=True)
class RunIdentity:
    user_id: UUID
    run_id: UUID
    thread_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class RunResult:
    final_output: AnswerQuestionResult
    # 記録を追加できなかったRun(direct_answer含む)ではNone。書き込み側はNoneを
    # 「触らない」と解釈し、threadの既存handoffをそのまま残す。
    research_handoff: ResearchHandoff | None = None


class AnsweringRunError(ApplicationError):
    """1回のリサーチの実行が、Runを止める失敗で回答を作れなかった。"""

    def __init__(
        self,
        *,
        cause: PlanningError
        | DirectAnswerError
        | EvidenceAnswerError
        | AIProviderError,
    ) -> None:
        self.cause = cause
        self.__cause__ = cause
        super().__init__("リサーチの実行が工程の失敗で止まりました")


class AnsweringRunner:
    def __init__(
        self,
        *,
        phases_factory: AnsweringPhasesFactory,
        stage_gate: StageGate,
        events: AnswerEventReporter | None = None,
    ) -> None:
        self._phases_factory = phases_factory
        self._stage_gate = stage_gate
        self._events = events

    async def run(
        self,
        input: RunInput,
        *,
        identity: RunIdentity,
    ) -> RunResult | CannotContinue:
        with logfire.span(_SPAN_NAME, run_id=str(identity.run_id)) as run_span:
            try:
                previous_answer = _latest_assistant_answer(input.history)
                phases = self._phases_factory()

                match await self._stage_gate.enter("planning"):
                    case CanContinue():
                        pass
                    case (
                        CancelledByUser()
                        | DeadlineExceeded()
                        | AttemptSuperseded()
                        | RunNotFound()
                    ) as stopped:
                        return stopped
                planning_input = PlanningInput(
                    question=input.question,
                    history=input.history,
                    as_of=identity.as_of,
                    research_handoff=input.research_handoff,
                )
                answering_request = AnsweringRequest(
                    question=input.question,
                    history=input.history,
                    as_of=identity.as_of,
                )
                plan = await phases.planner.plan(planning_input)
                match plan:
                    case DirectAnswerPlan():
                        return await self._answer_directly(
                            phases=phases,
                            request=answering_request,
                            previous_answer=previous_answer,
                        )
                    case SearchPlan():
                        match await self._stage_gate.enter("evidence_collection"):
                            case CanContinue():
                                pass
                            case (
                                CancelledByUser()
                                | DeadlineExceeded()
                                | AttemptSuperseded()
                                | RunNotFound()
                            ) as stopped:
                                return stopped
                        collected_news = await phases.collector.collect(
                            plan=plan,
                            as_of=answering_request.as_of,
                        )
                        match await self._review_evidence(
                            phases=phases,
                            collected_news=collected_news,
                            as_of=answering_request.as_of,
                        ):
                            case (
                                EvidenceRunCompleted() | EvidenceRunFailed()
                            ) as evidence_run:
                                pass
                            case (
                                CancelledByUser()
                                | DeadlineExceeded()
                                | AttemptSuperseded()
                                | RunNotFound()
                            ) as stopped:
                                return stopped
                        _record_evidence_run_span_attributes(
                            run_span,
                            collected_news=collected_news,
                            evidence_run=evidence_run,
                        )
                        handoff_input = ResearchHandoffInput.from_run(
                            previous=input.research_handoff,
                            question=answering_request.question,
                            collected_news=collected_news,
                            evidence_run=evidence_run,
                            as_of=answering_request.as_of,
                        )
                        return await self._answer_while_organizing(
                            phases=phases,
                            request=answering_request,
                            plan=plan,
                            collected_news=collected_news,
                            evidence_run=evidence_run,
                            run_span=run_span,
                            handoff_input=handoff_input,
                        )
                    case _ as unreachable:
                        assert_never(unreachable)
            # LLM呼び出し口の開閉で起きるAIの失敗は工程の外なので、ここで包む。
            except (
                PlanningError,
                DirectAnswerError,
                EvidenceAnswerError,
                AIProviderError,
            ) as failure:
                raise AnsweringRunError(cause=failure) from failure

    async def _answer_while_organizing(
        self,
        *,
        phases: AnsweringPhases,
        request: AnsweringRequest,
        plan: SearchPlan,
        collected_news: CollectedNews,
        evidence_run: EvidenceRunResult,
        run_span: LogfireSpan,
        handoff_input: ResearchHandoffInput | None,
    ) -> RunResult | CannotContinue:
        """回答のストリーミングと並行して、次のリサーチで必要な情報を整理する

        整理が間に合わない・失敗した場合は調査の履歴だけを残す。Runが止まったときと
        例外では並行taskをcancelして合流してから、止まった理由を返すか元の例外を送出する。
        """
        if handoff_input is None:
            match await self._answer_from_evidence(
                phases=phases,
                request=request,
                plan=plan,
                collected_news=collected_news,
                evidence_run=evidence_run,
                run_span=run_span,
            ):
                case AnswerQuestionResult() as answer:
                    return RunResult(final_output=answer, research_handoff=None)
                case (
                    CancelledByUser()
                    | DeadlineExceeded()
                    | AttemptSuperseded()
                    | RunNotFound()
                ) as stopped:
                    return stopped

        organizing = asyncio.ensure_future(phases.organizer.organize(handoff_input))
        try:
            answered = await self._answer_from_evidence(
                phases=phases,
                request=request,
                plan=plan,
                collected_news=collected_news,
                evidence_run=evidence_run,
                run_span=run_span,
            )
        except BaseException:
            # cancel・回答失敗。整理を畳んで合流してから元の例外を送出する。
            await _cancel_and_join(organizing)
            raise
        match answered:
            case AnswerQuestionResult() as answer:
                pass
            case (
                CancelledByUser()
                | DeadlineExceeded()
                | AttemptSuperseded()
                | RunNotFound()
            ) as stopped:
                await _cancel_and_join(organizing)
                return stopped

        try:
            research_handoff = await asyncio.wait_for(
                organizing,
                HANDOFF_ORGANIZE_TIMEOUT_SECONDS,
            )
        except Exception:
            # 回答は確定済み。整理を諦めて台帳だけ残す(CancelledErrorは貫通する)。
            research_handoff = handoff_input.handoff
        finally:
            await _cancel_and_join(organizing)
        return RunResult(final_output=answer, research_handoff=research_handoff)

    async def _answer_directly(
        self,
        *,
        phases: AnsweringPhases,
        request: AnsweringRequest,
        previous_answer: str,
    ) -> RunResult | CannotContinue:
        match await phases.direct_answerer.answer(
            DirectAnswerInput(request=request, previous_answer=previous_answer)
        ):
            case DirectAnswerDraft(answer=answer):
                return RunResult(
                    final_output=AnswerQuestionResult(
                        status="answered",
                        answer=answer,
                        sources=[],
                        missing_aspects=[],
                        plan_summary=AnswerPlanSummary(plan_type="direct_answer"),
                    ),
                    research_handoff=None,
                )
            case (
                CancelledByUser()
                | DeadlineExceeded()
                | AttemptSuperseded()
                | RunNotFound()
            ) as stopped:
                return stopped

    async def _review_evidence(
        self,
        *,
        phases: AnsweringPhases,
        collected_news: CollectedNews,
        as_of: datetime,
    ) -> EvidenceRunResult | CannotContinue:
        """ヒットゼロのRunは精査を開始せず、stageもselected eventにも反映されない。"""
        if not collected_news.has_hits:
            return EvidenceRunCompleted(
                answer_evidence=AnswerEvidence(),
                review_missing=(),
            )

        match await self._stage_gate.enter("evidence_review"):
            case CanContinue():
                pass
            case (
                CancelledByUser()
                | DeadlineExceeded()
                | AttemptSuperseded()
                | RunNotFound()
            ) as stopped:
                return stopped
        evidence_run = await phases.reviewer.review(
            tasks=collected_news.tasks,
            as_of=as_of,
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
    ) -> AnswerQuestionResult | CannotContinue:
        if isinstance(evidence_run, EvidenceRunCompleted):
            answer_evidence = evidence_run.answer_evidence
            review_missing = evidence_run.review_missing
        else:
            answer_evidence = AnswerEvidence()
            review_missing = ()
        evidence = build_answer_input_evidence(answer_evidence)

        match await phases.evidence_answerer.answer(
            EvidenceAnswerInput(
                request=request,
                evidence=tuple(evidence),
                target_time_window=plan.target_time_window,
                review_missing=review_missing,
            )
        ):
            case EvidenceAnswerDraft() as answer_outcome:
                pass
            case (
                CancelledByUser()
                | DeadlineExceeded()
                | AttemptSuperseded()
                | RunNotFound()
            ) as stopped:
                return stopped
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


async def _cancel_and_join(task: asyncio.Future[ResearchHandoff]) -> None:
    """未了なら畳み、完了を待ってから戻る。task内の例外はここで捨てる。"""
    if task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


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
    span.set_attribute("research_task_count", len(collected_news.tasks))
    if isinstance(evidence_run, EvidenceRunFailed):
        span.set_attribute("review_failure_code", evidence_run.failure_code)


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
