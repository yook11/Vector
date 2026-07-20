"""Probe question answering retrieval/synthesis and direct answer paths."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.direct_answer.flow import DirectAnswerFlow
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow
from app.agent.composition import (
    activate_gemini_agent_runtime,
    build_external_research_runtime_factory,
)
from app.agent.contract import (
    AnswerProgressEvent,
    AnswerQuestionResult,
    AnswerSource,
    ExternalSearchCandidatesFetchedEvent,
    ExternalSearchEvidenceSelectedEvent,
    ExternalSearchQueriesGeneratedEvent,
)
from app.agent.evidence_collection.internal_search import InternalSearchQueries
from app.agent.evidence_collection.internal_search.article_search import (
    InternalArticleSearchHit,
)
from app.agent.input_safety.agent import INPUT_SAFETY_AGENT
from app.agent.input_safety.service import InputSafetyService
from app.agent.planning.contract import (
    ExternalResearchTask,
    ExternalSearchPlan,
    NoRetrievalPlan,
    PlanningRequest,
)
from app.agent.question_context.agent import QUESTION_CONTEXT_AGENT
from app.agent.question_context.service import QuestionContextService
from app.agent.running import AnsweringPhases, AnsweringRunner, RunContext, RunInput
from app.config import settings

DEFAULT_GOAL = "NVIDIA Blackwell AI GPU latest supply and customer demand evidence"
DEFAULT_QUESTION = "NVIDIA Blackwell の直近の供給と顧客需要は投資判断に重要？"
DEFAULT_DIRECT_QUESTION = "Vector の使い方を短く教えて"
MAX_EXTERNAL_RESEARCH_TASKS = 3


class _UnreachableInternalSearch:
    async def search_articles(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalArticleSearchHit]:
        raise AssertionError(f"internal search must not be called: {queries!r}")


class _FixedExternalPlanner:
    def __init__(self, plan: ExternalSearchPlan) -> None:
        self._plan = plan

    async def plan(self, request: PlanningRequest) -> ExternalSearchPlan:  # noqa: ARG002
        return self._plan


class _FixedDirectPlanner:
    def __init__(self, plan: NoRetrievalPlan) -> None:
        self._plan = plan

    async def plan(self, request: PlanningRequest) -> NoRetrievalPlan:  # noqa: ARG002
        return self._plan


class _RecordingAnswerEvents:
    def __init__(self) -> None:
        self.events: list[AnswerProgressEvent] = []

    async def event_occurred(self, event: AnswerProgressEvent) -> None:
        self.events.append(event)


class _UnreachableDirectAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        previous_answer: str = "",  # noqa: ARG002
    ) -> DirectAnswerDraft:
        raise AssertionError(
            "direct answerer must not be called: "
            f"{request.context.standalone_question!r}"
        )


class _UnreachableExternalRuntimeFactory:
    def activate(self) -> object:
        raise AssertionError("external runtime must not activate")


class _UnreachableEvidenceAnswerer:
    async def answer(
        self,
        *,
        request: AnsweringRequest,
        evidence: list[AnswerEvidenceItem],
        target_time_window: str | None,  # noqa: ARG002
    ) -> EvidenceAnswerDraft:
        raise AssertionError(
            "evidence answerer must not be called: "
            f"{request.context.standalone_question!r}, {evidence!r}"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe AnsweringRunner external retrieval or direct answer path."
    )
    parser.add_argument(
        "--mode",
        choices=("external", "direct"),
        default="external",
        help="Probe mode. Defaults to external.",
    )
    parser.add_argument(
        "goals",
        nargs="*",
        metavar="goal",
        help=(
            "External research collection_goal for --mode external. Quote each goal "
            "containing spaces. At most 3 goals are accepted."
        ),
    )
    parser.add_argument(
        "--agents",
        type=int,
        default=1,
        help="Requested external search agent count. Defaults to 1.",
    )
    parser.add_argument(
        "--time-window",
        default=None,
        help="Optional external plan target_time_window value.",
    )
    parser.add_argument(
        "--question",
        default=None,
        help="Question passed to AnsweringRunner.",
    )
    return parser


async def _probe(
    *,
    mode: str,
    question: str,
    goals: Sequence[str],
    requested_agent_count: int,
    target_time_window: str | None,
) -> None:
    if mode == "direct":
        await _probe_direct(question=question)
        return
    await _probe_external(
        question=question,
        goals=goals,
        requested_agent_count=requested_agent_count,
        target_time_window=target_time_window,
    )


async def _probe_external(
    *,
    question: str,
    goals: Sequence[str],
    requested_agent_count: int,
    target_time_window: str | None,
) -> None:
    _require_secret("TAVILY_API_KEY", settings.tavily_api_key.get_secret_value())
    _require_secret("DEEPSEEK_API_KEY", settings.deepseek_api_key.get_secret_value())
    _require_secret("GEMINI_API_KEY", settings.gemini_api_key.get_secret_value())

    as_of = datetime.now(UTC)
    plan = _build_external_plan(goals, target_time_window=target_time_window)
    events = _RecordingAnswerEvents()
    runner = AnsweringRunner(
        input_safety_checker=InputSafetyService(
            agent=INPUT_SAFETY_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
        ),
        context_preparer=QuestionContextService(
            agent=QUESTION_CONTEXT_AGENT,
            runtime_scope_factory=None,
        ),
        phases_factory=lambda: AnsweringPhases(
            planner=_FixedExternalPlanner(plan),
            internal_search=_UnreachableInternalSearch(),
            external_runtime_factory=build_external_research_runtime_factory(),
            evidence_answerer=EvidenceAnswerFlow(
                agent=EVIDENCE_ANSWER_AGENT,
                runtime_scope_factory=activate_gemini_agent_runtime,
            ),
            direct_answerer=_UnreachableDirectAnswerer(),
        ),
        events=events,
        requested_external_agent_count=requested_agent_count,
    )
    result = (
        await runner.run(
            RunInput(question=question, history=()),
            run_context=RunContext(run_id=uuid4(), as_of=as_of),
        )
    ).final_output

    _print_retrieval_summary(
        as_of=as_of,
        plan=plan,
        requested_agent_count=requested_agent_count,
        events=events.events,
        collection_failures=result.retrieval.collection_failures,
    )
    print()
    _print_answer_result(result)


async def _probe_direct(*, question: str) -> None:
    _require_secret("GEMINI_API_KEY", settings.gemini_api_key.get_secret_value())

    as_of = datetime.now(UTC)
    runner = AnsweringRunner(
        input_safety_checker=InputSafetyService(
            agent=INPUT_SAFETY_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
        ),
        context_preparer=QuestionContextService(
            agent=QUESTION_CONTEXT_AGENT,
            runtime_scope_factory=None,
        ),
        phases_factory=lambda: AnsweringPhases(
            planner=_FixedDirectPlanner(NoRetrievalPlan(reason="direct answer probe")),
            internal_search=_UnreachableInternalSearch(),
            external_runtime_factory=_UnreachableExternalRuntimeFactory(),
            evidence_answerer=_UnreachableEvidenceAnswerer(),
            direct_answerer=DirectAnswerFlow(
                agent=DIRECT_ANSWER_AGENT,
                runtime_scope_factory=activate_gemini_agent_runtime,
            ),
        ),
    )
    result = (
        await runner.run(
            RunInput(question=question, history=()),
            run_context=RunContext(run_id=uuid4(), as_of=as_of),
        )
    ).final_output

    print("direct:")
    print(f"  as_of={as_of.isoformat()}")
    print("  planned_mode=none")
    print()
    _print_answer_result(result)


def _require_secret(name: str, value: str) -> None:
    if not value:
        raise SystemExit(f"{name} is not configured")


def _build_external_plan(
    goals: Sequence[str],
    *,
    target_time_window: str | None,
) -> ExternalSearchPlan:
    cleaned_goals = [goal.strip() for goal in goals if goal.strip()]
    if not cleaned_goals:
        cleaned_goals = [DEFAULT_GOAL]
    if len(cleaned_goals) > MAX_EXTERNAL_RESEARCH_TASKS:
        raise SystemExit(
            f"external research goals must be at most {MAX_EXTERNAL_RESEARCH_TASKS}"
        )

    return ExternalSearchPlan(
        external_research_tasks=[
            ExternalResearchTask(collection_goal=goal) for goal in cleaned_goals
        ],
        target_time_window=target_time_window,
        reason="external retrieval probe",
    )


def _print_retrieval_summary(
    *,
    as_of: datetime,
    plan: ExternalSearchPlan,
    requested_agent_count: int,
    events: Sequence[AnswerProgressEvent],
    collection_failures: Sequence[str],
) -> None:
    print("retrieval:")
    print(f"  as_of={as_of.isoformat()}")
    print(f"  planned_mode={plan.retrieval_mode}")
    print(f"  target_time_window={plan.target_time_window or ''}")
    print(f"  requested_agent_count={requested_agent_count}")
    print(f"  planned_task_count={len(plan.external_research_tasks)}")
    print(f"  collection_failures={list(collection_failures)}")
    print()
    _print_external_progress(events)


def _print_external_progress(events: Sequence[AnswerProgressEvent]) -> None:
    print("task_progress:")
    external_events = [
        event
        for event in events
        if isinstance(
            event,
            ExternalSearchQueriesGeneratedEvent
            | ExternalSearchCandidatesFetchedEvent
            | ExternalSearchEvidenceSelectedEvent,
        )
    ]
    if not external_events:
        print("  (none)")
        return

    for event in external_events:
        match event:
            case ExternalSearchQueriesGeneratedEvent():
                print(f"  [{event.task_index}] generated_queries={list(event.queries)}")
            case ExternalSearchCandidatesFetchedEvent():
                print(f"  [{event.task_index}] candidate_count={event.candidate_count}")
            case ExternalSearchEvidenceSelectedEvent():
                print(f"  [{event.task_index}] evidence_count={event.evidence_count}")


def _print_answer_result(result: AnswerQuestionResult) -> None:
    print("answer:")
    print(f"  status={result.status}")
    print(f"  answer={result.answer}")
    print(f"  missing_aspects={list(result.missing_aspects)}")
    print(f"  collection_failures={list(result.retrieval.collection_failures)}")
    print("  sources:")
    if not result.sources:
        print("    (none)")
        return
    for source in result.sources:
        _print_answer_source(source)


def _print_answer_source(source: AnswerSource) -> None:
    print(f"    [{source.source_ref}] kind={source.kind}")
    print(f"        title={source.title}")
    url = getattr(source, "url", None)
    if url is not None:
        print(f"        url={url}")
    article_id = getattr(source, "article_id", None)
    if article_id is not None:
        print(f"        article_id={article_id}")
    source_name = getattr(source, "source_name", None)
    print(f"        source_name={source_name or ''}")
    print(f"        published_at={_format_datetime(source.published_at)}")
    evidence_claim = getattr(source, "evidence_claim", None)
    if evidence_claim:
        print(f"        evidence_claim={evidence_claim}")


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    question = args.question or (
        DEFAULT_DIRECT_QUESTION if args.mode == "direct" else DEFAULT_QUESTION
    )
    asyncio.run(
        _probe(
            mode=args.mode,
            question=question,
            goals=args.goals,
            requested_agent_count=args.agents,
            target_time_window=args.time_window,
        )
    )


if __name__ == "__main__":
    main()
